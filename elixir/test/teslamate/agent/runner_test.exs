defmodule TeslaMate.Agent.RunnerTest do
  # Application env is process-global, so these tests do not run concurrently.
  use ExUnit.Case, async: false

  alias TeslaMate.Agent.{Runner}
  alias TeslaMate.AgentSnapshotFixture

  defmodule Stub do
    @moduledoc false
    def chat(messages, _config) do
      Process.put(:last_messages, messages)

      case Process.get(:stub_responses, []) do
        [] ->
          {:error, :unexpected_call}

        [next | rest] ->
          Process.put(:stub_responses, rest)
          next
      end
    end
  end

  setup do
    previous = Application.get_env(:teslamate, :agent_client)
    Application.put_env(:teslamate, :agent_client, Stub)

    on_exit(fn ->
      case previous do
        nil -> Application.delete_env(:teslamate, :agent_client)
        value -> Application.put_env(:teslamate, :agent_client, value)
      end
    end)

    :ok
  end

  test "calls a tool, then answers with the tool's figures" do
    events =
      run([
        tool_call("overview", %{"days" => 30}),
        answer("You drove 150 km over 2 drives.")
      ])

    assert [
             {:tool, "overview", summary},
             {:answer, "You drove 150 km over 2 drives."},
             {:meta, %{model: "deepseek-flash", tokens: 15}},
             :done
           ] = events

    assert summary =~ "read 2 drives"
  end

  test "draws the chart before the answer and sends only the config back to the model" do
    events =
      run([
        tool_call("propose_chart", %{
          "chart" => "setup_comparison",
          "kind" => "bar",
          "aspect" => "tyres"
        }),
        answer("The chart below compares your tyres.")
      ])

    assert [{:tool, "propose_chart", _}, {:chart, chart}, {:answer, _}, {:meta, _}, :done] =
             events

    assert chart.title == "Consumption by tyres"
    assert chart.svg =~ "<svg"
    # The SVG is not in the tool message the model reads: the payload the stub
    # was handed must not contain it.
    refute inspect(Process.get(:last_messages)) =~ "<svg"
  end

  test "runs several tools in one turn" do
    events =
      run([
        tool_calls([
          %{"id" => "a", "function" => %{"name" => "overview", "arguments" => ~s({"days":30})}},
          %{"id" => "b", "function" => %{"name" => "charging", "arguments" => ~s({"days":30})}}
        ]),
        answer("Done.")
      ])

    names = for {:tool, name, _} <- events, do: name
    assert names == ["overview", "charging"]
    assert {:answer, "Done."} in events
  end

  test "a provider error becomes a sentence the reader can act on" do
    events = run([{:error, :rate_limited}])

    assert [{:error, message}, :done] = events
    assert message =~ "busy"
  end

  test "synthesises the answer after a tool-heavy fourth turn instead of apologising" do
    events =
      run([
        tool_call("overview", %{}),
        tool_call("overview", %{}),
        tool_call("overview", %{}),
        tool_call("overview", %{}),
        answer("Here is the answer from what I read.")
      ])

    assert {:answer, "Here is the answer from what I read."} = List.keyfind(events, :answer, 0)
    assert Enum.count(events, fn event -> match?({:tool, _, _}, event) end) == 4
    assert {:meta, %{tokens: 45}} = List.keyfind(events, :meta, 0)
    assert List.last(events) == :done
  end

  test "keeps the fallback sentence when even the forced synthesis returns nothing" do
    events =
      run([
        tool_call("overview", %{}),
        tool_call("overview", %{}),
        tool_call("overview", %{}),
        tool_call("overview", %{}),
        answer("")
      ])

    assert {:answer, answer} = List.keyfind(events, :answer, 0)
    assert answer =~ "could not put that answer together"
  end

  test "answers every tool call the budget refuses, so the provider turn stays complete" do
    five_calls =
      for index <- 1..5 do
        %{
          "id" => "call_#{index}",
          "type" => "function",
          "function" => %{"name" => "overview", "arguments" => "{}"}
        }
      end

    events = run([tool_calls(five_calls), answer("Done with four.")])

    assert Enum.count(events, fn event -> match?({:tool, _, _}, event) end) == 4
    assert {:answer, "Done with four."} = List.keyfind(events, :answer, 0)

    # The fifth call still got a tool message, otherwise the next request would
    # be an assistant turn with an unanswered tool_call.
    messages = Process.get(:last_messages)
    tool_messages = Enum.filter(messages, &(&1["role"] == "tool"))
    assert length(tool_messages) == 5
    assert Enum.any?(tool_messages, &String.contains?(&1["content"], "tool budget"))
  end

  test "refuses a second chart in one answer" do
    events =
      run([
        tool_call("propose_chart", %{"chart" => "monthly_distance", "kind" => "bar"}),
        tool_call("propose_chart", %{"chart" => "charging_energy", "kind" => "bar"}),
        answer("Two charts would be a gallery.")
      ])

    charts = Enum.filter(events, &match?({:chart, _}, &1))
    assert length(charts) == 1
  end

  # -- helpers --------------------------------------------------------------

  defp run(responses) do
    Process.put(:stub_responses, responses)
    Process.delete(:last_messages)
    parent = self()

    Runner.run(
      AgentSnapshotFixture.snapshot(),
      [%{role: "user", content: "How did I drive?"}],
      %{
        model: "deepseek-flash",
        tools: [],
        max_tokens: 100,
        timeout_ms: 1000,
        api_key: "test",
        base_url: "http://localhost"
      },
      fn event -> send(parent, {:event, event}) end
    )

    collect([])
  end

  defp collect(acc) do
    receive do
      {:event, :done} -> Enum.reverse([:done | acc])
      {:event, event} -> collect([event | acc])
    after
      1000 -> Enum.reverse(acc)
    end
  end

  defp answer(text, tokens \\ 5) do
    {:ok,
     %{
       "choices" => [%{"message" => %{"content" => text}}],
       "usage" => %{"total_tokens" => tokens}
     }}
  end

  defp tool_call(name, args, tokens \\ 10) do
    tool_calls(
      [
        %{
          "id" => "call_1",
          "type" => "function",
          "function" => %{"name" => name, "arguments" => Jason.encode!(args)}
        }
      ],
      tokens
    )
  end

  defp tool_calls(calls, tokens \\ 10) do
    {:ok,
     %{
       "choices" => [%{"message" => %{"content" => "", "tool_calls" => calls}}],
       "usage" => %{"total_tokens" => tokens}
     }}
  end
end
