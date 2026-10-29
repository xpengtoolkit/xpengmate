defmodule TeslaMateWeb.XpengMateLive.Index do
  @moduledoc """
  Settings for the XpengMate half of the stack: which live source the engine
  uses, the XPENG credentials when it authenticates itself, and the EVConduit
  history feed with how old the imported data is.

  Everything here talks to the two local services over the compose network; no
  secret is ever rendered back - the engine reports only whether one is set.
  """

  use TeslaMateWeb, :live_view

  alias TeslaMate.XpengMate.Client

  use Gettext, backend: TeslaMateWeb.Gettext

  on_mount {TeslaMateWeb.InitAssigns, :locale}

  @upload_keys [:ca_chain, :client_cert, :client_key, :tenant, :tokens]

  @impl true
  def mount(_params, _session, socket) do
    socket =
      socket
      |> assign(
        page_title: gettext("XpengMate"),
        loading: true,
        hydrating: false,
        pending_save: false,
        engine: nil,
        history: nil,
        exports: nil,
        test_result: nil,
        active_section: "source"
      )
      |> assign(source_form: to_form(source_params(%{}), as: :source))
      |> assign(credentials_form: to_form(%{"vin" => ""}, as: :credentials))
      |> assign(history_form: to_form(history_params(%{}), as: :history))
      |> assign(agent_form: to_form(agent_form_params(), as: :agent), agent_test: nil)
      |> allow_upload(:ca_chain, accept: :any, max_entries: 1, max_file_size: 2_000_000)
      |> allow_upload(:client_cert, accept: :any, max_entries: 1, max_file_size: 2_000_000)
      |> allow_upload(:client_key, accept: :any, max_entries: 1, max_file_size: 2_000_000)
      |> allow_upload(:tenant, accept: :any, max_entries: 1, max_file_size: 2_000_000)
      |> allow_upload(:tokens, accept: :any, max_entries: 1, max_file_size: 2_000_000)

    # Fetched for the static render too: both services are on the compose
    # network, and a settings page that paints its status in the first frame is
    # worth a few milliseconds of render time.
    {:ok, assign_status(socket)}
  end

  # -- load / refresh ----------------------------------------

  @impl true
  def handle_info(:reload, socket) do
    {:noreply, assign_status(socket)}
  end

  @impl true
  def handle_event("refresh", _params, socket) do
    {:noreply, assign_status(socket)}
  end

  # The two services are on the compose network, so these calls are fast. Doing
  # them in the LiveView process (rather than start_async) keeps the first
  # rendered frame complete instead of showing a placeholder that fills in.
  defp assign_status(socket) do
    engine = fetch_engine()
    history = fetch_history()
    exports = fetch_exports()

    socket
    |> assign(
      loading: false,
      engine: engine,
      history: history,
      exports: exports,
      agent: TeslaMate.Agent.status()
    )
    |> maybe_hydrate()
    |> schedule_refresh(history, exports)
  end

  # First paint hydrates the forms; after a save the persisted values must win
  # over what the user typed, which is how the page proves the change stuck.
  defp maybe_hydrate(%{assigns: %{hydrating: false}} = socket), do: hydrate_forms(socket)
  defp maybe_hydrate(%{assigns: %{pending_save: true}} = socket), do: hydrate_forms(socket)
  defp maybe_hydrate(socket), do: socket

  defp schedule_refresh(socket, history, exports) do
    history_running = match?(%{"running" => true}, history)
    exports_running = match?(%{"running" => true}, exports)

    if history_running or exports_running do
      Process.send_after(self(), :reload, 5_000)
    end

    socket
  end

  # -- source ------------------------------------------------

  @impl true
  def handle_event("validate_source", %{"source" => params}, socket) do
    {:noreply, assign(socket, source_form: to_form(params, as: :source))}
  end

  def handle_event("save_source", %{"source" => params}, socket) do
    payload =
      %{
        "source" => params["source"],
        "vin" => params["vin"],
        "usable_battery_kwh" => params["usable_battery_kwh"],
        "provider_url" => params["provider_url"],
        "provider_token" => params["provider_token"],
        "ha_url" => params["ha_url"],
        "ha_token" => params["ha_token"],
        "ha_entity_prefix" => params["ha_entity_prefix"]
      }
      |> Enum.reject(fn {_key, value} -> value in [nil, ""] end)
      |> Map.new()

    case Client.put_engine_config(payload) do
      {:ok, _} ->
        # The engine exits and compose restarts it; poll briefly so the form
        # re-hydrates from what was actually persisted.
        Process.send_after(self(), :reload, 2_500)
        Process.send_after(self(), :reload, 7_000)

        {:noreply,
         socket
         |> assign(source_form: to_form(params, as: :source), pending_save: true)
         |> put_flash(
           :info,
           gettext("Saved. The engine is restarting on the new source.")
         )}

      {:error, reason} ->
        {:noreply, put_flash(socket, :error, reason)}
    end
  end

  def handle_event("test_source", params, socket) do
    params = Map.merge(socket.assigns.source_form.params, Map.drop(params, ["value", "_target"]))

    payload =
      %{
        "source" => params["source"],
        "provider_url" => params["provider_url"],
        "provider_token" => params["provider_token"],
        "ha_url" => params["ha_url"],
        "ha_token" => params["ha_token"],
        "ha_entity_prefix" => params["ha_entity_prefix"]
      }
      |> Enum.reject(fn {_key, value} -> value in [nil, ""] end)
      |> Map.new()

    case Client.test_source(payload) do
      {:ok, result} ->
        {:noreply, assign(socket, test_result: result)}

      {:error, reason} ->
        {:noreply, assign(socket, test_result: %{"ok" => false, "error" => reason})}
    end
  end

  # -- credentials -------------------------------------------

  def handle_event("validate_uploads", _params, socket) do
    # LiveView tracks the selected files itself; this handler only exists so the
    # upload form has a change event.
    {:noreply, socket}
  end

  def handle_event("save_credentials", %{"credentials" => params}, socket) do
    uploads = Map.new(@upload_keys, fn key -> {key, read_upload(socket, key)} end)
    missing = for {key, value} <- uploads, value in [nil, ""], do: key

    if missing == [] do
      payload = %{
        "ca_chain" => uploads[:ca_chain],
        "client_cert" => uploads[:client_cert],
        "client_key" => uploads[:client_key],
        "tenant" => uploads[:tenant],
        "tokens" => uploads[:tokens],
        "vin" => params["vin"]
      }

      case Client.install_credentials(payload) do
        {:ok, _} ->
          Process.send_after(self(), :reload, 2_500)
          Process.send_after(self(), :reload, 7_000)

          {:noreply,
           socket
           |> assign(pending_save: true)
           |> put_flash(
             :info,
             gettext("Credentials installed. The engine is restarting with them.")
           )}

        {:error, reason} ->
          {:noreply, put_flash(socket, :error, reason)}
      end
    else
      {:noreply,
       put_flash(
         socket,
         :error,
         gettext("Every file is required: %{keys}", keys: Enum.join(missing, ", "))
       )}
    end
  end

  def handle_event("cancel_upload", params, socket) do
    atom = safe_upload_key(params["key"] || "")
    ref = params["ref"] || :all
    {:noreply, cancel_upload(socket, atom, ref)}
  end

  # -- history -----------------------------------------------

  def handle_event("validate_history", %{"history" => params}, socket) do
    {:noreply, assign(socket, history_form: to_form(params, as: :history))}
  end

  def handle_event("save_history", %{"history" => params}, socket) do
    payload =
      %{
        "mode" => params["mode"],
        "supabase_url" => params["supabase_url"],
        "service_key" => params["service_key"],
        "api_url" => params["api_url"],
        "api_key" => params["api_key"],
        "vin" => params["vin"],
        "export_zip" => params["export_zip"],
        "usable_battery_kwh" => params["usable_battery_kwh"],
        "car_name" => params["car_name"]
      }
      |> Enum.reject(fn {_key, value} -> value in [nil, ""] end)
      |> Map.new()

    case Client.put_history_config(payload) do
      {:ok, _} ->
        Process.send_after(self(), :reload, 1_500)

        {:noreply,
         socket
         |> assign(history_form: to_form(params, as: :history))
         |> put_flash(:info, gettext("History source saved."))}

      {:error, reason} ->
        {:noreply, put_flash(socket, :error, reason)}
    end
  end

  def handle_event("sync_history", _params, socket) do
    case Client.sync_history() do
      {:ok, _} ->
        Process.send_after(self(), :reload, 3_000)

        {:noreply,
         put_flash(socket, :info, gettext("History sync started; this page refreshes itself."))}

      {:error, reason} ->
        {:noreply, put_flash(socket, :error, reason)}
    end
  end

  # -- assistant ---------------------------------------------

  def handle_event("validate_agent", %{"agent" => params}, socket) do
    {:noreply, assign(socket, agent_form: to_form(params, as: :agent))}
  end

  def handle_event("save_agent", %{"agent" => params}, socket) do
    attrs = %{
      "api_key" => params["api_key"],
      "model" => params["model"],
      "base_url" => params["base_url"],
      "daily_limit" => params["daily_limit"],
      "enabled" => params["enabled"] in ["true", "on", "1"]
    }

    case TeslaMate.Agent.update_settings(attrs) do
      {:ok, _settings} ->
        {:noreply,
         socket
         |> assign(
           agent_form: to_form(agent_form_params(), as: :agent),
           agent: TeslaMate.Agent.status(),
           agent_test: nil
         )
         |> put_flash(:info, gettext("Assistant settings saved."))}

      {:error, %Ecto.Changeset{} = changeset} ->
        {:noreply,
         socket
         |> assign(agent_form: to_form(params, as: :agent))
         |> put_flash(:error, assistant_error(changeset))}
    end
  end

  def handle_event("test_agent", _params, socket) do
    params = socket.assigns.agent_form.params

    result =
      TeslaMate.Agent.verify_key(%{
        "api_key" => params["api_key"],
        "base_url" => params["base_url"]
      })

    {:noreply, assign(socket, agent_test: result)}
  end

  def handle_event("clear_agent_key", _params, socket) do
    {:ok, _settings} = TeslaMate.Agent.clear_api_key()

    {:noreply,
     socket
     |> assign(
       agent_form: to_form(agent_form_params(), as: :agent),
       agent: TeslaMate.Agent.status(),
       agent_test: nil
     )
     |> put_flash(:info, gettext("API key removed."))}
  end

  # -- helpers -----------------------------------------------

  defp fetch_engine do
    case Client.engine_status() do
      {:ok, status} -> status
      {:error, reason} -> {:error, reason}
    end
  end

  defp fetch_history do
    case Client.history_status() do
      {:ok, status} -> status
      {:error, reason} -> {:error, reason}
    end
  end

  defp fetch_exports do
    case Client.exports_status() do
      {:ok, status} -> status
      {:error, reason} -> {:error, reason}
    end
  end

  defp hydrate_forms(socket) do
    source =
      case socket.assigns.engine do
        %{"config" => config} when is_map(config) ->
          %{
            "source" => config["source"] || "provider",
            "vin" => config["vin"] || "",
            "usable_battery_kwh" => to_string(config["usable_battery_kwh"] || "87.5"),
            "provider_url" => config["provider_url"] || "",
            "provider_token" => "",
            "ha_url" => config["ha_url"] || "",
            "ha_token" => "",
            "ha_entity_prefix" => config["ha_entity_prefix"] || "",
            "outside_temp_entity" => config["outside_temp_entity"] || ""
          }

        _ ->
          nil
      end

    history =
      case socket.assigns.history do
        %{"config" => config} when is_map(config) ->
          %{
            "mode" => config["mode"] || "api",
            "supabase_url" => config["supabase_url"] || "",
            "service_key" => "",
            "api_url" => config["api_url"] || "",
            "api_key" => "",
            "vin" => config["vin"] || "",
            "export_zip" => config["export_zip"] || "",
            "usable_battery_kwh" => to_string(config["usable_battery_kwh"] || "87.5"),
            "car_name" => config["car_name"] || ""
          }

        _ ->
          nil
      end

    socket
    |> then(fn socket ->
      if source, do: assign(socket, source_form: to_form(source, as: :source)), else: socket
    end)
    |> then(fn socket ->
      if history, do: assign(socket, history_form: to_form(history, as: :history)), else: socket
    end)
    |> assign(hydrating: true, pending_save: false)
  end

  defp source_params(params) do
    Map.merge(
      %{
        "source" => "ha",
        "vin" => "",
        "usable_battery_kwh" => "87.5",
        "provider_url" => "",
        "provider_token" => "",
        "ha_url" => "",
        "ha_token" => "",
        "ha_entity_prefix" => ""
      },
      params
    )
  end

  defp history_params(params) do
    Map.merge(
      %{
        "mode" => "supabase",
        "supabase_url" => "",
        "service_key" => "",
        "api_url" => "",
        "api_key" => "",
        "vin" => "",
        "export_zip" => "",
        "usable_battery_kwh" => "87.5",
        "car_name" => ""
      },
      params
    )
  end

  defp agent_form_params do
    settings = TeslaMate.Agent.get_settings()

    %{
      "api_key" => "",
      "model" => settings.model || "deepseek-flash",
      "base_url" => settings.base_url || "https://api.deepseek.com",
      "daily_limit" => to_string(settings.daily_limit || 60),
      "enabled" => if(settings.enabled, do: "true", else: "false")
    }
  end

  defp assistant_error(%Ecto.Changeset{} = changeset) do
    messages =
      changeset
      |> Ecto.Changeset.traverse_errors(fn {message, opts} ->
        Enum.reduce(opts, message, fn {key, value}, acc ->
          String.replace(acc, "%{#{key}}", to_string(value))
        end)
      end)
      |> Enum.flat_map(fn {field, errors} ->
        Enum.map(errors, fn error -> "#{field}: #{error}" end)
      end)

    gettext("Could not save the assistant settings: %{errors}", errors: Enum.join(messages, "; "))
  end

  defp key_source_label(:environment), do: gettext("from the environment")
  defp key_source_label(:database), do: gettext("stored encrypted in the database")
  defp key_source_label(_), do: gettext("not set")

  defp read_upload(socket, key) do
    case consume_uploaded_entries(socket, key, fn %{path: path}, _entry ->
           {:ok, File.read!(path)}
         end) do
      [content | _] -> content
      _ -> nil
    end
  end

  defp safe_upload_key(key) do
    Enum.find(@upload_keys, :ca_chain, &(Atom.to_string(&1) == key))
  end

  defp epoch_text(nil), do: "—"

  defp epoch_text(seconds) when is_integer(seconds) do
    case DateTime.from_unix(seconds) do
      {:ok, value} -> Calendar.strftime(value, "%Y-%m-%d %H:%M")
      _ -> "—"
    end
  end

  defp epoch_text(_), do: "—"

  defp age_text(nil), do: gettext("never")

  defp age_text(hours) when is_number(hours) and hours < 1 do
    gettext("%{minutes} min ago", minutes: max(1, round(hours * 60)))
  end

  defp age_text(hours) when is_number(hours) and hours < 48 do
    gettext("%{hours} h ago", hours: round(hours))
  end

  defp age_text(hours) when is_number(hours) do
    gettext("%{days} days ago", days: round(hours / 24))
  end

  defp age_text(other), do: to_string(other)

  defp fmt(value) when value in [nil, ""], do: "—"
  defp fmt(value) when is_binary(value), do: value
  defp fmt(value), do: to_string(value)

  defp source_label("provider"), do: gettext("Home Assistant provider (xpengtoolkit 0.6.16+)")
  defp source_label("ha"), do: gettext("Home Assistant entities (fallback)")
  defp source_label("xpeng"), do: gettext("XPENG cloud (native engine)")
  defp source_label(other), do: to_string(other)
end
