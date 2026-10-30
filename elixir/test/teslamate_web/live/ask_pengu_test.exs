defmodule TeslaMateWeb.AskPenguTest do
  use TeslaMateWeb.ConnCase

  import Mock

  alias TeslaMate.Agent

  @configured %{
    enabled: true,
    configured: true,
    model: "deepseek-flash",
    daily_limit: 60,
    used: 3,
    key_source: :database
  }

  # Same stub shape the Ask page tests use: status is a fixture, ask records the
  # call and reports a live process instead of starting a real provider loop.
  defp with_agent(status, fun) do
    test_pid = self()

    ask = fn messages, opts ->
      send(test_pid, {:ask_called, messages, opts})
      {:ok, test_pid}
    end

    with_mock Agent, [:passthrough], status: fn -> status end, ask: ask do
      fun.()
    end
  end

  defp eventually(fun, attempts \\ 40) do
    if fun.() do
      :ok
    else
      if attempts == 0, do: flunk("condition never became true")
      Process.sleep(25)
      eventually(fun, attempts - 1)
    end
  end

  test "sits idle with the first-visit hint", %{conn: conn} do
    with_agent(@configured, fn ->
      {:ok, view, html} = live(conn, "/ask")

      assert html =~ ~s(data-state="idle")
      assert html =~ "ask me about your car"
      assert has_element?(view, ~s(#pengu.pengu[data-state="idle"]))
    end)
  end

  test "wakes while you type and settles when the field is emptied", %{conn: conn} do
    with_agent(@configured, fn ->
      {:ok, view, _html} = live(conn, "/ask")

      assert render_change(view, :update_question, %{"content" => "How did I drive"}) =~
               ~s(data-state="listening")

      assert render_change(view, :update_question, %{"content" => ""}) =~ ~s(data-state="idle")
    end)
  end

  test "thinks while the answer runs, talks when it lands, then goes quiet", %{conn: conn} do
    with_agent(@configured, fn ->
      {:ok, view, _html} = live(conn, "/ask")

      render_submit(view, :ask, %{"content" => "How did I drive?"})
      assert has_element?(view, ~s(#pengu[data-state="thinking"]))
      assert_receive {:ask_called, _messages, _opts}

      send(view.pid, {:agent_event, {:progress, "Reading your data…"}})
      html = render(view)
      assert html =~ ~s(data-state="thinking")
      assert html =~ "Reading your data"

      send(view.pid, {:agent_event, {:answer, "You drove 150 km."}})
      assert render(view) =~ ~s(data-state="speaking")

      send(view.pid, {:agent_event, :done})
      assert render(view) =~ ~s(data-state="speaking")

      eventually(fn -> render(view) =~ ~s(data-state="idle") end)
    end)
  end

  test "goes dizzy when the answer fails", %{conn: conn} do
    with_agent(@configured, fn ->
      {:ok, view, _html} = live(conn, "/ask")

      render_submit(view, :ask, %{"content" => "How did I drive?"})
      send(view.pid, {:agent_event, {:error, "The provider exploded."}})

      assert render(view) =~ ~s(data-state="error")
    end)
  end

  test "a Hey Pengu greeting is not part of the question", %{conn: conn} do
    with_agent(@configured, fn ->
      {:ok, view, _html} = live(conn, "/ask")

      render_submit(view, :ask, %{"content" => "Hey Pengu, how did I drive?"})

      assert_receive {:ask_called, messages, _opts}
      assert List.last(messages).content == "how did I drive?"
    end)
  end

  test "a greeting on its own wakes it up and asks for the question", %{conn: conn} do
    with_agent(@configured, fn ->
      {:ok, view, _html} = live(conn, "/ask")

      html = render_submit(view, :ask, %{"content" => "Hey Pengu"})

      assert html =~ "I&#39;m listening" or html =~ "I'm listening"
      refute_received {:ask_called, _, _}
    end)
  end
end
