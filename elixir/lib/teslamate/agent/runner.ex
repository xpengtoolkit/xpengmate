defmodule TeslaMate.Agent.Runner do
  @moduledoc """
  The tool-calling loop: at most four model turns and nine tool calls per
  question.

  The limits are the cost ceiling, not a tuning knob: a model that keeps asking
  for another slice of the same data instead of answering would otherwise spend
  money until it happened to stop. A second chart in one answer is refused for
  the same reason - it usually means the model lost the thread.

  Events are emitted through a function rather than tied to a process, so the
  same loop can be tested with a collector and run from a Task for the page.
  """

  alias TeslaMate.Agent.{Client, Prompt, Tools}

  @max_turns 4
  @max_tool_calls_per_turn 4
  @max_tool_calls_total 9

  @fallback_answer "I could not put that answer together from the data. Try asking about a specific period."

  @doc "Runs one answer. `emit` is called with every event, ending with `:done`."
  def run(snapshot, messages, config, emit) when is_function(emit, 1) do
    state = %{
      snapshot: snapshot,
      config: config,
      emit: emit,
      chat: [%{"role" => "system", "content" => Prompt.system(snapshot)} | normalise(messages)],
      turns: 0,
      calls: 0,
      charts: [],
      tokens: 0,
      answer: nil
    }

    loop(state)
  end

  defp loop(%{turns: turns} = state) when turns >= @max_turns, do: synthesise(state)

  # The turn ceiling is about cost, not about leaving the reader with nothing:
  # the model has already gathered tool results, so one more call with the
  # tools removed forces it to write the answer it owes.
  defp synthesise(state) do
    config = %{state.config | tools: []}

    note = %{
      "role" => "system",
      "content" =>
        "You have the tool results above and no tools now. Answer the question in words using those results; do not request more tools."
    }

    case client().chat(state.chat ++ [note], config) do
      {:ok, response} ->
        tokens = state.tokens + token_count(response)

        case response do
          %{"choices" => [%{"message" => %{"content" => content}} | _]}
          when is_binary(content) and content != "" ->
            finish(%{state | answer: String.trim(content), tokens: tokens})

          _ ->
            finish(%{state | tokens: tokens})
        end

      {:error, reason} ->
        emit(state, {:error, Client.error_message(reason)})
        emit(state, :done)
    end
  end

  defp loop(state) do
    case client().chat(state.chat, state.config) do
      {:ok, response} ->
        respond(state, response)

      {:error, reason} ->
        emit(state, {:error, Client.error_message(reason)})
        emit(state, :done)
    end
  end

  defp respond(state, response) do
    tokens = state.tokens + token_count(response)

    case response do
      %{"choices" => [%{"message" => message} | _]} ->
        state = %{state | tokens: tokens, turns: state.turns + 1}

        case message["tool_calls"] do
          calls when is_list(calls) and calls != [] ->
            state
            |> append_assistant(message)
            |> run_tool_calls(calls)
            |> loop()

          _ ->
            finish(%{state | answer: String.trim(message["content"] || "")})
        end

      _ ->
        emit(state, {:error, "The AI provider returned no answer."})
        emit(state, :done)
    end
  end

  defp token_count(%{"usage" => %{"total_tokens" => tokens}}) when is_integer(tokens), do: tokens
  defp token_count(_), do: 0

  defp append_assistant(state, message) do
    %{
      state
      | chat:
          state.chat ++
            [
              %{
                "role" => "assistant",
                "content" => message["content"] || "",
                "tool_calls" => message["tool_calls"]
              }
            ]
    }
  end

  defp run_tool_calls(state, calls) do
    calls
    |> Enum.with_index()
    |> Enum.reduce(state, fn {call, index}, state ->
      if state.calls >= @max_tool_calls_total or index >= @max_tool_calls_per_turn do
        append_skipped(state, call)
      else
        run_tool_call(state, call)
      end
    end)
  end

  defp append_skipped(state, %{"function" => function, "id" => id}) do
    name = function["name"] || ""

    payload =
      Jason.encode!(%{
        ok: false,
        error:
          "Not executed: this answer has used its tool budget. Answer the question with the results you already have."
      })

    %{
      state
      | chat:
          state.chat ++ [%{"role" => "tool", "tool_call_id" => id || name, "content" => payload}]
    }
  end

  defp append_skipped(state, _call), do: state

  defp run_tool_call(state, %{"function" => function, "id" => id}) do
    name = function["name"] || ""
    args = parse_args(function["arguments"])

    result =
      if name == "propose_chart" and state.charts != [] do
        %{
          ok: false,
          error: "A chart has already been proposed for this answer. Answer in words now."
        }
      else
        Tools.execute(state.snapshot, name, args)
      end

    summary = Map.get(result, :_summary, "")
    chart = Map.get(result, :_chart)
    payload = result |> Map.drop([:_summary, :_chart]) |> Jason.encode!()

    if summary != "", do: emit(state, {:tool, name, summary})

    state = %{
      state
      | calls: state.calls + 1,
        charts: if(chart, do: state.charts ++ [chart], else: state.charts),
        chat:
          state.chat ++ [%{"role" => "tool", "tool_call_id" => id || name, "content" => payload}]
    }

    state
  end

  defp run_tool_call(state, _call), do: state

  defp parse_args(nil), do: %{}

  defp parse_args(raw) when is_binary(raw) do
    case Jason.decode(raw) do
      {:ok, args} when is_map(args) -> args
      _ -> %{}
    end
  end

  defp parse_args(raw) when is_map(raw), do: raw
  defp parse_args(_), do: %{}

  defp finish(state) do
    answer =
      case state.answer do
        answer when is_binary(answer) and answer != "" -> answer
        _ -> @fallback_answer
      end

    Enum.each(state.charts, fn chart ->
      emit(state, {:chart, %{title: chart.title, svg: chart.svg}})
    end)

    emit(state, {:answer, answer})
    emit(state, {:meta, %{model: state.config.model, tokens: state.tokens}})
    emit(state, :done)
  end

  defp emit(state, event), do: state.emit.(event)

  defp client do
    Application.get_env(:teslamate, :agent_client, TeslaMate.Agent.Client.HTTP)
  end

  defp normalise(messages) do
    Enum.map(messages, fn message ->
      %{
        "role" => to_string(message[:role] || message["role"] || "user"),
        "content" => to_string(message[:content] || message["content"] || "")
      }
    end)
  end
end
