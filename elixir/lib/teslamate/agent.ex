defmodule TeslaMate.Agent do
  @moduledoc """
  The "Ask" assistant: a tool-calling loop over the owner's own car data.

  Three decisions shape everything here:

  * **The model never touches the database.** It gets one bounded snapshot per
    question and a set of tools that are pure functions of it, so a figure it
    quotes is a figure a tool computed.
  * **The switch and the key are the owner's.** The assistant is off until it is
    turned on, the key is stored encrypted in the database and never rendered
    back, and a daily cap bounds the spend.
  * **A failure is a sentence, not a stack trace.** Every provider and tool
    error maps to wording the reader can act on.

  This module is the page's whole API: status, settings, and `ask/2`, which
  starts the loop in a supervised task and sends the events to the calling
  process.
  """

  require Logger

  alias TeslaMate.{Repo, Setups}
  alias TeslaMate.Agent.{Client, Runner, Settings, Snapshot, Usage}

  # The composer refuses a question longer than this; the server refuses it too.
  @max_question 1000
  @max_messages 10
  @max_total_chars 24_000

  @default_daily_limit 60
  @default_max_tokens 3000
  @default_timeout_seconds 60

  ## Status

  @doc """
  What the page and the settings form need to know, in one map.
  """
  def status do
    settings = get_settings()

    %{
      enabled: enabled?(settings),
      configured: configured?(settings),
      model: model(settings),
      daily_limit: settings.daily_limit || @default_daily_limit,
      used: used_today(),
      key_source: key_source(settings),
      daily_limit_reached: used_today() >= (settings.daily_limit || @default_daily_limit)
    }
  end

  def used_today, do: Usage.used_today()

  @doc "Whether the assistant is switched on (the env kill switch included)."
  def enabled?(settings \\ nil) do
    settings = settings || get_settings()

    truthy(settings.enabled) and
      case System.get_env("XPENGMATE_AGENT_ENABLED") do
        nil -> true
        value -> String.downcase(value) not in ["0", "false", "no", "off"]
      end
  end

  @doc "Whether a provider key is available at all."
  def configured?(settings \\ nil) do
    settings = settings || get_settings()
    is_binary(effective_key(settings)) and effective_key(settings) != ""
  end

  defp key_source(settings) do
    cond do
      present?(System.get_env("DEEPSEEK_API_KEY")) -> :environment
      is_binary(effective_key(settings)) and effective_key(settings) != "" -> :database
      true -> nil
    end
  end

  ## Settings

  def get_settings do
    Repo.get_by(Settings, singleton: true) ||
      case Repo.insert(%Settings{singleton: true}) do
        {:ok, settings} -> settings
        # Another request inserted the row first; read it back.
        {:error, _} -> Repo.get_by!(Settings, singleton: true)
      end
  end

  def change_settings(attrs \\ %{}) do
    get_settings()
    |> Settings.changeset(attrs)
  end

  @doc """
  Saves settings. A blank API key means "leave the stored one alone", so the
  form can round-trip without the key ever being sent back to the browser.
  """
  def update_settings(attrs) do
    get_settings()
    |> Settings.changeset(drop_blank_key(attrs))
    |> Repo.update()
  end

  @doc "Removes the stored key (the environment's key, if any, still applies)."
  def clear_api_key do
    get_settings()
    |> Ecto.Changeset.change(api_key: nil, enabled: false)
    |> Repo.update()
  end

  defp drop_blank_key(attrs) do
    attrs
    |> Enum.reject(fn {key, value} -> to_string(key) == "api_key" and value in [nil, ""] end)
    |> Map.new()
  end

  @doc """
  Checks the stored key against the provider without spending a question.

  Returns `:ok` or `{:error, message}`.
  """
  def verify_key(attrs \\ %{}) do
    settings = get_settings()
    key = attrs[:api_key] || attrs["api_key"]
    base = attrs[:base_url] || attrs["base_url"] || settings.base_url

    Client.verify(%{
      api_key: if(present?(key), do: key, else: effective_key(settings)),
      base_url: base || "https://api.deepseek.com"
    })
  end

  ## Asking

  @doc """
  Validates the conversation the page is about to send.

  A question is capped at #{@max_question} characters; the whole transcript at
  #{@max_total_chars}. The answer is not capped at the question length - the page
  replays its own answers as history - so the two limits are separate.
  """
  def validate_messages(messages) do
    messages = Enum.map(messages, &normalise_message/1)

    total = messages |> Enum.map(&byte_size(&1["content"])) |> Enum.sum()

    cond do
      messages == [] ->
        {:error, :empty_question}

      Enum.any?(messages, fn message -> message["role"] not in ["user", "assistant"] end) ->
        {:error, :invalid_messages}

      List.last(messages)["role"] != "user" ->
        {:error, :invalid_messages}

      Enum.any?(messages, fn message -> String.trim(message["content"]) == "" end) ->
        {:error, :empty_question}

      Enum.any?(messages, fn message -> byte_size(message["content"]) > 8000 end) ->
        {:error, :conversation_too_long}

      List.last(messages) |> Map.get("content") |> String.length() > @max_question ->
        {:error, :question_too_long}

      length(messages) > @max_messages or total > @max_total_chars ->
        {:error, :conversation_too_long}

      true ->
        :ok
    end
  end

  @doc """
  Starts one question and returns as soon as the answer is running.

  Events are sent to `opts[:notify]` (default: the caller) as
  `{:agent_event, event}`, ending with `:done`. The question is counted before
  the provider runs, so a failure still costs its slot: a counter that only
  counted successes would let a client loop on errors for free.
  """
  def ask(messages, opts \\ []) do
    with :ok <- validate_messages(messages),
         :ok <- ensure_enabled(),
         config = effective_config(),
         :ok <- ensure_configured(config),
         {:ok, _used} <- consume_usage() do
      notify = Keyword.get(opts, :notify, self())
      tz = Keyword.get_lazy(opts, :tz, &Setups.timezone/0)
      # `snapshot:` is a test seam (and a future cache): the page never passes it,
      # so a question normally reads the database inside the task.
      snapshot = Keyword.get(opts, :snapshot)
      messages = Enum.map(messages, &normalise_message/1)

      # The supervisor is in the application's children; when the app is not
      # running (the test suite starts only the Repo), a plain task keeps the
      # same behaviour.
      run = fn -> run_question(messages, config, tz, notify, snapshot) end

      case Process.whereis(TeslaMate.TaskSupervisor) do
        nil -> Task.start(run)
        _pid -> Task.Supervisor.start_child(TeslaMate.TaskSupervisor, run)
      end
    end
  end

  defp run_question(messages, config, tz, notify, snapshot) do
    send_event(notify, {:progress, "Reading your data…"})

    try do
      snapshot = snapshot || Snapshot.load(tz: tz)
      Runner.run(snapshot, messages, config, &send_event(notify, &1))
    rescue
      error ->
        Logger.error("[agent] question failed: #{Exception.message(error)}")
        send_event(notify, {:error, "Something went wrong while reading the data. Try again."})
        send_event(notify, :done)
    end
  end

  defp send_event(pid, event), do: send(pid, {:agent_event, event})

  ## Errors

  @doc "The reader-facing sentence for a reason `ask/2` returned."
  def error_message(:disabled),
    do: "The assistant is switched off. Turn it on on the XpengMate settings page."

  def error_message(:not_configured),
    do: "The assistant has no API key yet. Add one on the XpengMate settings page."

  def error_message(:daily_limit),
    do: "You have used today's questions. The allowance resets at midnight UTC."

  def error_message(:empty_question), do: "Type a question first."

  def error_message(:question_too_long),
    do: "That question is too long. Keep it under a few sentences."

  def error_message(:conversation_too_long),
    do: "This conversation is too long. Clear it and start a new question."

  def error_message(:invalid_messages),
    do: "That conversation could not be sent. Reload the page and try again."

  def error_message(reason), do: "The assistant is not available right now (#{inspect(reason)})."

  ## Effective configuration

  @doc "The provider configuration, with environment variables taking priority."
  def effective_config do
    settings = get_settings()

    %{
      api_key: effective_key(settings),
      base_url: base_url(settings),
      model: model(settings),
      max_tokens: max_tokens(),
      timeout_ms: timeout_seconds() * 1000,
      tools: TeslaMate.Agent.Tools.schemas()
    }
  end

  defp effective_key(settings) do
    case System.get_env("DEEPSEEK_API_KEY") do
      key when is_binary(key) and key != "" -> key
      _ -> settings.api_key
    end
  end

  defp base_url(settings) do
    case System.get_env("DEEPSEEK_BASE_URL") do
      url when is_binary(url) and url != "" -> String.trim_trailing(url, "/")
      _ -> settings.base_url || "https://api.deepseek.com"
    end
  end

  defp model(settings) do
    case System.get_env("DEEPSEEK_MODEL") do
      model when is_binary(model) and model != "" -> model
      _ -> settings.model || "deepseek-flash"
    end
  end

  defp max_tokens do
    case Integer.parse(System.get_env("XPENGMATE_AGENT_MAX_TOKENS") || "") do
      {tokens, _} when tokens > 0 -> tokens
      _ -> @default_max_tokens
    end
  end

  defp timeout_seconds do
    case Float.parse(System.get_env("XPENGMATE_AGENT_TIMEOUT_SECONDS") || "") do
      {seconds, _} when seconds > 0 -> trunc(seconds)
      _ -> @default_timeout_seconds
    end
  end

  defp ensure_enabled do
    if enabled?(), do: :ok, else: {:error, :disabled}
  end

  defp ensure_configured(config) do
    if is_binary(config.api_key) and config.api_key != "",
      do: :ok,
      else: {:error, :not_configured}
  end

  defp consume_usage do
    limit = get_settings().daily_limit || @default_daily_limit
    Usage.consume(limit)
  end

  ## Helpers

  defp normalise_message(message) do
    %{
      "role" => to_string(message[:role] || message["role"] || "user"),
      "content" => to_string(message[:content] || message["content"] || "")
    }
  end

  defp present?(value), do: is_binary(value) and value != ""
  defp truthy(nil), do: false
  defp truthy(value), do: value
end
