defmodule TeslaMate.XpengMate.Client do
  @moduledoc """
  The two local services behind the XpengMate settings page.

  Both live on the compose network and authenticate with the same provider
  token xpengmate already uses. Neither is reachable from outside the stack.
  """

  require Logger

  @engine_default "http://engine:8202"
  @history_default "http://history:8100"

  def engine_url, do: System.get_env("XPENGMATE_ENGINE_ADMIN_URL", @engine_default)
  def history_url, do: System.get_env("XPENGMATE_HISTORY_URL", @history_default)
  def token, do: System.get_env("XPENGMATE_ADMIN_TOKEN", "")

  # -- engine -------------------------------------------------

  def engine_status, do: request(:get, engine_url() <> "/status", nil)

  def engine_config, do: request(:get, engine_url() <> "/config", nil)

  def put_engine_config(settings) when is_map(settings),
    do: request(:put, engine_url() <> "/config", settings)

  def install_credentials(payload) when is_map(payload),
    do: request(:post, engine_url() <> "/credentials", payload)

  def test_source(payload) when is_map(payload),
    do: request(:post, engine_url() <> "/test", payload)

  def restart_engine, do: request(:post, engine_url() <> "/restart", %{})

  # -- history ------------------------------------------------

  def history_status, do: request(:get, history_url() <> "/status", nil)

  def history_config, do: request(:get, history_url() <> "/config", nil)

  def put_history_config(settings) when is_map(settings),
    do: request(:put, history_url() <> "/config", settings)

  def sync_history, do: request(:post, history_url() <> "/sync", %{})

  # -- transport ----------------------------------------------

  defp client do
    Tesla.client(
      [
        {Tesla.Middleware.Headers,
         [
           {"authorization", "Bearer " <> token()},
           {"content-type", "application/json"}
         ]},
        Tesla.Middleware.JSON
      ],
      {Tesla.Adapter.Finch, name: TeslaMate.HTTP, receive_timeout: 25_000}
    )
  end

  defp request(method, url, body) do
    result =
      case method do
        :get -> Tesla.get(client(), url)
        :post -> Tesla.post(client(), url, body || %{})
        :put -> Tesla.put(client(), url, body || %{})
        :delete -> Tesla.delete(client(), url)
      end

    case result do
      {:ok, %Tesla.Env{status: status, body: body}} when status in 200..299 ->
        {:ok, body}

      {:ok, %Tesla.Env{status: status, body: body}} ->
        message =
          case body do
            %{"error" => error} -> error
            other -> inspect(other)
          end

        Logger.info("XpengMate settings call failed: #{method} #{url} -> #{status} #{message}")
        {:error, message}

      {:error, reason} ->
        Logger.info("XpengMate settings call unreachable: #{method} #{url} -> #{inspect(reason)}")
        {:error, "service unreachable"}
    end
  end
end
