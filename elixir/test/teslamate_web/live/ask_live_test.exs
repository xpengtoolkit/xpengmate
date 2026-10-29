defmodule TeslaMateWeb.AskLiveTest do
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

  @not_configured %{
    enabled: true,
    configured: false,
    model: "deepseek-flash",
    daily_limit: 60,
    used: 0,
    key_source: nil
  }

  @disabled %{
    enabled: false,
    configured: true,
    model: "deepseek-flash",
    daily_limit: 60,
    used: 0,
    key_source: :database
  }

  @exhausted %{
    enabled: true,
    configured: true,
    model: "deepseek-flash",
    daily_limit: 2,
    used: 2,
    key_source: :database
  }

  # Stubs the core so the page's state machine can be tested deterministically:
  # `status/0` returns the given map and `ask/2` records the call and reports a
  # process that stays alive for the duration of the test (instead of starting a
  # real provider loop).
  defp with_agent(status, fun) do
    test_pid = self()

    ask = fn messages, opts ->
      send(test_pid, {:ask_called, messages, opts})
      {:ok, test_pid}
    end

    with_agent(status, ask, fun)
  end

  defp with_agent(status, ask, fun) do
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

  describe "page states" do
    test "shows the title, usage, model and the permanent data warning", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, _view, html} = live(conn, "/ask")

        assert html =~ "Ask"
        assert html =~ "3 of 60 questions used today"
        assert html =~ "deepseek-flash"
        assert html =~ "Your data leaves this server"
        assert html =~ "DeepSeek"
        assert html =~ "read-only"
        # The warning is permanent: there is no dismiss/opt-in control.
        refute html =~ "dismiss"
      end)
    end

    test "unconfigured page links to XpengMate and disables the composer", %{conn: conn} do
      test_pid = self()

      ask = fn _messages, _opts ->
        send(test_pid, :ask_called)
        {:ok, test_pid}
      end

      with_agent(@not_configured, ask, fn ->
        assert {:ok, view, html} = live(conn, "/ask")

        assert html =~ "No API key configured"
        assert html =~ "/xpengmate"
        assert has_element?(view, "#ask-question[disabled]")
        assert has_element?(view, "#ask-submit[disabled]")

        render_submit(view, :ask, %{"content" => "How is my battery?"})

        refute_received :ask_called
        assert render(view) =~ "Add a DeepSeek API key on the XpengMate page"
      end)
    end

    test "disabled page explains why it cannot be used", %{conn: conn} do
      with_agent(@disabled, fn ->
        assert {:ok, view, html} = live(conn, "/ask")

        assert html =~ "Ask is switched off"
        assert has_element?(view, "#ask-question[disabled]")
        assert has_element?(view, "#ask-suggestions button[disabled]")
      end)
    end

    test "an exhausted daily limit shows the cap and blocks submitting", %{conn: conn} do
      with_agent(@exhausted, fn ->
        assert {:ok, view, html} = live(conn, "/ask")

        assert html =~ "Daily limit reached"
        assert html =~ "2 of 2 questions used today"
        assert has_element?(view, "#ask-question[disabled]")
        assert has_element?(view, "#ask-submit[disabled]")

        render_submit(view, :ask, %{"content" => "How is my battery?"})

        refute_received {:ask_called, _, _}
        assert render(view) =~ "You have used today"
      end)
    end
  end

  describe "asking" do
    test "submits the question, clears the composer and calls ask/2", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")

        render_submit(view, :ask, %{"content" => "How is my battery?"})

        assert_received {:ask_called, messages, opts}
        assert [%{role: "user", content: "How is my battery?"}] = messages
        assert is_binary(Keyword.fetch!(opts, :tz))
        assert Keyword.fetch!(opts, :notify) == view.pid

        html = render(view)
        assert html =~ "How is my battery?"
        assert html =~ "Thinking…"
        assert has_element?(view, "#ask-question[disabled]")
        assert has_element?(view, "#ask-submit[disabled]")
      end)
    end

    test "a too-long question is rejected without calling ask/2", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")

        render_submit(view, :ask, %{"content" => String.duplicate("a", 1_001)})

        refute_received {:ask_called, _, _}
        assert render(view) =~ "Questions are limited to 1000 characters"
      end)
    end

    test "an empty question is rejected without calling ask/2", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")

        render_submit(view, :ask, %{"content" => "   "})

        refute_received {:ask_called, _, _}
        assert render(view) =~ "Type a question first"
      end)
    end

    test "a second submit cannot start while a question is running", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")

        render_submit(view, :ask, %{"content" => "first"})
        assert_received {:ask_called, _, _}

        render_submit(view, :ask, %{"content" => "second"})

        refute_received {:ask_called, _, _}
        assert render(view) =~ "still answering"
      end)
    end

    test "a suggestion submits its text as the question", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")

        view
        |> element("#ask-suggestions button", "What does my battery health look like?")
        |> render_click()

        assert_received {:ask_called, [%{role: "user", content: content}], _opts}
        assert content == "What does my battery health look like?"
      end)
    end
  end

  describe "streamed events" do
    test "progress and a markdown answer render in the conversation", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")
        render_submit(view, :ask, %{"content" => "How is my battery?"})

        send(view.pid, {:agent_event, {:progress, "Reading your data…"}})
        assert render(view) =~ "Reading your data…"

        send(view.pid, {:agent_event, {:answer, "**Bold** answer with `code`"}})
        html = render(view)

        assert html =~ "<strong>Bold</strong>"
        assert html =~ "<code>code</code>"
      end)
    end

    test "tool events render their summaries under the answer", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")
        render_submit(view, :ask, %{"content" => "How is my battery?"})

        send(view.pid, {:agent_event, {:tool, "drives", "last 30 days of drives"}})
        send(view.pid, {:agent_event, {:tool, "charges", "all DC charging sessions"}})
        html = render(view)

        assert html =~ "checked:"
        assert html =~ "last 30 days of drives"
        assert html =~ "all DC charging sessions"
      end)
    end

    test "chart events render the server-generated SVG", %{conn: conn} do
      svg = ~s{<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>}

      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")
        render_submit(view, :ask, %{"content" => "How is my battery?"})

        send(view.pid, {:agent_event, {:chart, %{title: "Monthly consumption", svg: svg}}})
        html = render(view)

        assert html =~ "Monthly consumption"
        assert html =~ "agent-chart"
        assert html =~ "<svg"
        assert html =~ "<rect"
      end)
    end

    test "a chart that is not an SVG is ignored", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")
        render_submit(view, :ask, %{"content" => "How is my battery?"})

        send(
          view.pid,
          {:agent_event, {:chart, %{title: "evil", svg: ~s{<script>alert("x")</script>}}}}
        )

        html = render(view)
        refute html =~ "<script>"
        refute html =~ "evil"
      end)
    end

    test "an error event renders the message and re-enables the composer", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")
        render_submit(view, :ask, %{"content" => "How is my battery?"})

        send(view.pid, {:agent_event, {:error, "The provider is down"}})

        html = render(view)
        assert html =~ "The provider is down"
        assert has_element?(view, "#ask-question:not([disabled])")
      end)
    end

    test ":done re-enables the composer", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")
        render_submit(view, :ask, %{"content" => "How is my battery?"})

        send(view.pid, {:agent_event, {:answer, "An answer"}})
        send(view.pid, {:agent_event, :done})

        html = render(view)
        assert html =~ "An answer"
        refute html =~ "Thinking…"
        assert has_element?(view, "#ask-question:not([disabled])")
        assert has_element?(view, "#ask-submit:not([disabled])")
      end)
    end

    test "a process that exits without :done reports an error", %{conn: conn} do
      test_pid = self()

      ask = fn messages, opts ->
        pid =
          spawn(fn ->
            receive do
              :stop -> :ok
            end
          end)

        send(test_pid, {:ask_called, messages, opts, pid})
        {:ok, pid}
      end

      with_agent(@configured, ask, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")
        render_submit(view, :ask, %{"content" => "How is my battery?"})

        assert_receive {:ask_called, _messages, _opts, pid}
        send(pid, :stop)

        eventually(fn -> render(view) =~ "stopped before it could answer" end)
        assert has_element?(view, "#ask-question:not([disabled])")
      end)
    end
  end

  describe "conversation" do
    test "clear conversation empties the list when idle", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")
        render_submit(view, :ask, %{"content" => "How is my battery?"})
        send(view.pid, {:agent_event, {:answer, "Your battery looks fine."}})
        send(view.pid, {:agent_event, :done})
        render(view)

        view |> element("#clear-conversation") |> render_click()

        html = render(view)
        refute html =~ "How is my battery?"
        refute html =~ "Your battery looks fine."

        assert [conversation] =
                 html
                 |> Floki.parse_document!()
                 |> Floki.find("#conversation")

        assert String.trim(Floki.text(conversation)) == ""
      end)
    end

    test "follow-up questions replay the recent history, trimming long answers", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")

        render_submit(view, :ask, %{"content" => "first question"})
        assert_received {:ask_called, _, _}

        long_answer = String.duplicate("a", 5_000)
        send(view.pid, {:agent_event, {:answer, long_answer}})
        send(view.pid, {:agent_event, :done})
        render(view)

        render_submit(view, :ask, %{"content" => "second question"})
        assert_received {:ask_called, messages, _}

        assert [
                 %{role: "user", content: "first question"},
                 %{role: "assistant", content: replayed},
                 %{role: "user", content: "second question"}
               ] = messages

        assert String.length(replayed) == 4_000
      end)
    end

    test "only the last ten turns are replayed", %{conn: conn} do
      with_agent(@configured, fn ->
        assert {:ok, view, _html} = live(conn, "/ask")

        for i <- 1..6 do
          render_submit(view, :ask, %{"content" => "question #{i}"})
          assert_received {:ask_called, _, _}
          send(view.pid, {:agent_event, {:answer, "answer #{i}"}})
          send(view.pid, {:agent_event, :done})
          render(view)
        end

        render_submit(view, :ask, %{"content" => "question 7"})
        assert_received {:ask_called, messages, _}

        # Ten replayed messages at most, starting with a question rather than a
        # dangling answer.
        assert length(messages) == 9
        assert [%{role: "user", content: "question 3"} | _] = messages
        assert %{role: "user", content: "question 7"} = List.last(messages)
      end)
    end
  end

  describe "integration with the real agent loop" do
    defmodule StubClient do
      @moduledoc false

      # An OpenAI-shaped completion: the loop only needs the message content.
      def chat(messages, _config) do
        question =
          messages
          |> Enum.reverse()
          |> Enum.find(fn message -> message["role"] == "user" end)

        {:ok,
         %{
           "choices" => [
             %{"message" => %{"content" => "You asked: **#{question["content"]}**"}}
           ],
           "usage" => %{"total_tokens" => 42}
         }}
      end
    end

    test "answers a question through Agent.ask/2 with a stubbed provider", %{conn: conn} do
      start_supervised!(TeslaMate.Vault)

      previous = Application.get_env(:teslamate, :agent_client)
      Application.put_env(:teslamate, :agent_client, StubClient)

      on_exit(fn ->
        if previous do
          Application.put_env(:teslamate, :agent_client, previous)
        else
          Application.delete_env(:teslamate, :agent_client)
        end
      end)

      assert {:ok, _settings} =
               Agent.update_settings(%{api_key: "sk-test-key", enabled: true, daily_limit: 60})

      assert {:ok, view, html} = live(conn, "/ask")
      refute html =~ "No API key configured"
      assert html =~ "deepseek-flash"

      render_submit(view, :ask, %{"content" => "How is my range?"})

      eventually(fn -> render(view) =~ "You asked:" end)

      html = render(view)
      assert html =~ "How is my range?"
      # The answer goes through the markdown renderer, not the model's raw HTML.
      assert html =~ "<strong>How is my range?</strong>"
      assert has_element?(view, "#ask-question:not([disabled])")
    end
  end
end
