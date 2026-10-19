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

  def exports_status, do: request(:get, history_url() <> "/exports", nil)

  @doc """
  Upload one or more XPENG export zips as multipart/form-data.

  The files can be several tens of MB, so this bypasses the JSON middleware and
  streams the raw body rather than base64-wrapping it.
  """
  def upload_exports(files) when is_list(files) and files != [] do
    boundary = "----xpengmate" <> Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)
    parts =
      for {name, content} <- files do
        [
          "--",
          boundary,
          "\r\n",
          "Content-Disposition: form-data; name=\"files\"; filename=\"",
          name,
          "\"\r\n",
          "Content-Type: application/zip\r\n\r\n",
          content,
          "\r\n"
        ]
      end

    body = IO.iodata_to_binary([parts, ["--", boundary, "--\r\n"]])

    client =
      Tesla.client(
        [{Tesla.Middleware.Headers, [{"authorization", "Bearer " <> token()}]}],
        {Tesla.Adapter.Finch, name: TeslaMate.HTTP, receive_timeout: 300_000}
      )

    case Tesla.post(client, history_url() <> "/exports", body,
           headers: [{"content-type", "multipart/form-data; boundary=" <> boundary}]
         ) do
      {:ok, %Tesla.Env{status: status, body: body}} when status in 200..299 ->
        {:ok, body}

      {:ok, %Tesla.Env{status: status, body: body}} ->
        {:error, failure_message(body, status)}

      {:error, reason} ->
        Logger.info("XpengMate export upload failed: #{inspect(reason)}")
        {:error, "history service unreachable"}
    end
  end

  # -- transport ----------------------------------------------

  defp client do
    Tesla.client(
      [
        {Tesla.Middleware.Headers, [{"authorization", "Bearer " <> token()}]},
        Tesla.Middleware.JSON
      ],
      {Tesla.Adapter.Finch, name: TeslaMate.HTTP, receive_timeout: 25_000}
    )
  end

  defp failure_message(body, status) do
    case body do
      %{"error" => error} -> error
      other -> "HTTP #{status}: #{inspect(other) |> String.slice(0, 200)}"
    end
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
        message = failure_message(body, status)
        Logger.info("XpengMate settings call failed: #{method} #{url} -> #{status} #{message}")
        {:error, message}

      {:error, reason} ->
        Logger.info("XpengMate settings call unreachable: #{method} #{url} -> #{inspect(reason)}")
        {:error, "service unreachable"}
    end
  end
end
