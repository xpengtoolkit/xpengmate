defmodule TeslaMateWeb.XpengMateController do
  @moduledoc """
  Plain multipart upload for XPENG data-export zips.

  The LiveView file input proved unreliable for large, multi-file selections
  (the first entry of a multi-select never reached the server), so the export
  card posts a normal multipart form here. Plug streams the parts to temp files,
  the controller forwards them to the history service, and the redirect carries
  the result back to the settings page.
  """

  use TeslaMateWeb, :controller

  require Logger

  alias TeslaMate.XpengMate.Client

  @max_files 12

  def upload_exports(conn, params) do
    files =
      params
      |> Map.get("files", [])
      |> List.wrap()
      |> Enum.reject(&(&1 in [nil, ""]))
      |> Enum.flat_map(fn
        %Plug.Upload{filename: filename, path: path} -> [{filename, File.read!(path)}]
        _ -> []
      end)

    cond do
      files == [] ->
        redirect_with(conn, :error, gettext("Choose at least one export zip first."))

      length(files) > @max_files ->
        redirect_with(
          conn,
          :error,
          gettext("Too many files: upload at most %{max} at a time.", max: @max_files)
        )

      true ->
        case Client.upload_exports(files) do
          {:ok, _} ->
            redirect_with(
              conn,
              :info,
              gettext(
                "Uploaded %{count} file(s). The import is running in the background; this page refreshes itself.",
                count: length(files)
              )
            )

          {:error, reason} ->
            Logger.info("XpengMate export upload rejected: #{inspect(reason)}")

            redirect_with(conn, :error, reason)
        end
    end
  end

  defp redirect_with(conn, kind, message) do
    conn
    |> put_flash(kind, message)
    |> redirect(to: "/xpengmate")
  end
end
