defmodule TeslaMate.Agent.Client do
  @moduledoc """
  The provider call: DeepSeek's OpenAI-compatible chat completions endpoint.

  Deliberately small and injectable. `TeslaMate.Agent` reads the module to call
  from `Application.get_env(:teslamate, :agent_client, TeslaMate.Agent.Client.HTTP)`,
  so the tests drive the whole agent loop with canned provider responses and no
  network, and a self-hoster can point `base_url` at any OpenAI-compatible
  gateway (Ollama, vLLM, Moonshot, OpenAI itself).

  Provider failures are mapped to reasons the page can explain to the reader;
  the provider's own body is logged, never shown (it can echo the prompt).
  """

  require Logger

  @type config :: %{
          api_key: String.t(),
          base_url: String.t(),
          model: String.t(),
          max_tokens: pos_integer(),
          timeout_ms: pos_integer(),
          tools: list()
        }

  @callback chat(messages :: [map()], config :: config) ::
              {:ok, map()} | {:error, atom() | {:http_error, non_neg_integer()}}

  defmodule HTTP do
    @behaviour TeslaMate.Agent.Client

    require Logger

    # A forced-synthesis call passes no tools; sending an empty array is not the
    # same request as sending none.
    defp with_tools(payload, []), do: payload
    defp with_tools(payload, tools), do: Map.merge(payload, %{tools: tools, tool_choice: "auto"})

    @impl true
    def chat(messages, config) do
      payload =
        %{
          model: config.model,
          messages: messages,
          temperature: 0.2,
          max_tokens: config.max_tokens,
          stream: false
        }
        |> with_tools(Map.get(config, :tools, []))

      url = String.trim_trailing(config.base_url, "/") <> "/chat/completions"

      headers = [
        {"content-type", "application/json"},
        {"authorization", "Bearer #{config.api_key}"}
      ]

      request = Finch.build(:post, url, headers, Jason.encode!(payload))

      case Finch.request(request, TeslaMate.HTTP, receive_timeout: config.timeout_ms) do
        {:ok, %Finch.Response{status: 200, body: body}} ->
          case Jason.decode(body) do
            {:ok, decoded} -> {:ok, decoded}
            {:error, _} -> {:error, :bad_response}
          end

        {:ok, %Finch.Response{status: 401}} ->
          Logger.error("[agent] provider rejected the API key")
          {:error, :unauthorized}

        {:ok, %Finch.Response{status: 429}} ->
          {:error, :rate_limited}

        {:ok, %Finch.Response{status: status, body: body}} ->
          Logger.error("[agent] provider returned #{status}: #{String.slice(body, 0, 300)}")
          {:error, {:http_error, status}}

        {:error, %Mint.TransportError{reason: :timeout}} ->
          {:error, :timeout}

        {:error, reason} ->
          Logger.warning("[agent] provider request failed: #{inspect(reason)}")
          {:error, :unreachable}
      end
    end
  end

  @doc """
  A cheap connectivity check for the settings page: `GET /models` with the key.

  It proves the key, the base URL and outbound network in one request, without
  spending a question.
  """
  def verify(%{api_key: api_key, base_url: base_url}) when is_binary(api_key) and api_key != "" do
    url = String.trim_trailing(base_url, "/") <> "/models"
    headers = [{"authorization", "Bearer #{api_key}"}]
    request = Finch.build(:get, url, headers)

    try do
      case Finch.request(request, TeslaMate.HTTP, receive_timeout: 15_000) do
        {:ok, %Finch.Response{status: status}} when status in 200..299 ->
          :ok

        {:ok, %Finch.Response{status: 401}} ->
          {:error, "The provider rejected this key."}

        {:ok, %Finch.Response{status: 404}} ->
          {:error, "The base URL does not look like an OpenAI-compatible endpoint (404)."}

        {:ok, %Finch.Response{status: status}} ->
          {:error, "The provider answered HTTP #{status}."}

        {:error, %Mint.TransportError{reason: :timeout}} ->
          {:error, "The provider did not answer in time."}

        {:error, reason} ->
          {:error, "Could not reach the provider: #{inspect(reason)}"}
      end
    rescue
      error -> {:error, "Could not reach the provider: #{Exception.message(error)}"}
    end
  end

  def verify(_config), do: {:error, "No API key is configured."}

  @doc "The reader-facing message for a provider failure."
  def error_message(:unauthorized),
    do: "The AI provider rejected this server's key. Check it on the XpengMate settings page."

  def error_message(:rate_limited),
    do: "The AI provider is busy right now. Try again in a minute."

  def error_message(:timeout),
    do: "The AI provider took too long. Try a shorter question."

  def error_message(:bad_response),
    do: "The AI provider returned something that could not be read."

  def error_message(_),
    do: "Could not reach the AI provider. Try again in a moment."
end
