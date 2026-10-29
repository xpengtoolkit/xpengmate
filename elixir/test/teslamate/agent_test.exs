defmodule TeslaMate.AgentTest do
  # The settings row and the daily counter are global, and the environment is
  # mutated to model a missing key, so these tests run one at a time.
  use TeslaMate.DataCase, async: false

  alias TeslaMate.Agent
  alias TeslaMate.AgentSnapshotFixture

  defmodule Stub do
    @moduledoc false
    def chat(_messages, _config) do
      {:ok,
       %{
         "choices" => [
           %{"message" => %{"content" => "You drove 150 km over two drives."}}
         ],
         "usage" => %{"total_tokens" => 7}
       }}
    end

    def verify(_config), do: :ok
  end

  setup do
    # The application is not booted in the test suite, so the Cloak vault that
    # encrypts the stored key has to be started for these tests.
    if Process.whereis(TeslaMate.Vault) == nil do
      start_supervised!(TeslaMate.Vault)
    end

    saved_key = System.get_env("DEEPSEEK_API_KEY")
    saved_enabled = System.get_env("XPENGMATE_AGENT_ENABLED")
    saved_client = Application.get_env(:teslamate, :agent_client)

    System.delete_env("DEEPSEEK_API_KEY")
    System.delete_env("XPENGMATE_AGENT_ENABLED")
    Application.put_env(:teslamate, :agent_client, Stub)

    on_exit(fn ->
      restore("DEEPSEEK_API_KEY", saved_key)
      restore("XPENGMATE_AGENT_ENABLED", saved_enabled)

      case saved_client do
        nil -> Application.delete_env(:teslamate, :agent_client)
        value -> Application.put_env(:teslamate, :agent_client, value)
      end
    end)

    :ok
  end

  defp restore(key, nil), do: System.delete_env(key)
  defp restore(key, value), do: System.put_env(key, value)

  describe "settings" do
    test "creates the single settings row on first read" do
      settings = Agent.get_settings()
      assert settings.singleton
      assert settings.model == "deepseek-flash"
      assert settings.enabled == false
      assert settings.daily_limit == 60
    end

    test "stores a key encrypted and never reports it in the status map" do
      assert {:ok, settings} =
               Agent.update_settings(%{
                 "api_key" => "sk-secret-value",
                 "enabled" => true,
                 "model" => "deepseek-chat",
                 "daily_limit" => 5
               })

      assert settings.api_key == "sk-secret-value"

      status = Agent.status()
      assert status.configured
      assert status.enabled
      assert status.model == "deepseek-chat"
      assert status.daily_limit == 5
      assert status.key_source == :database
      refute inspect(status) =~ "sk-secret-value"
      refute inspect(settings) =~ "sk-secret-value"
    end

    test "a blank key leaves the stored key alone" do
      {:ok, _} = Agent.update_settings(%{"api_key" => "sk-keep-me", "enabled" => true})

      assert {:ok, settings} = Agent.update_settings(%{"api_key" => "", "daily_limit" => 9})
      assert settings.api_key == "sk-keep-me"
      assert settings.daily_limit == 9
    end

    test "clearing the key turns the assistant off" do
      {:ok, _} = Agent.update_settings(%{"api_key" => "sk-x", "enabled" => true})
      assert {:ok, settings} = Agent.clear_api_key()

      refute settings.enabled
      refute Agent.configured?()
      refute Agent.status().configured
    end

    test "an environment key overrides the stored one and is reported as such" do
      System.put_env("DEEPSEEK_API_KEY", "sk-from-env")

      assert Agent.configured?()
      assert Agent.status().key_source == :environment
    end

    test "the environment switch can turn the assistant off" do
      {:ok, _} = Agent.update_settings(%{"api_key" => "sk-x", "enabled" => true})
      assert Agent.enabled?()

      System.put_env("XPENGMATE_AGENT_ENABLED", "false")
      refute Agent.enabled?()
    end
  end

  describe "validate_messages/1" do
    test "accepts a single question" do
      assert :ok = Agent.validate_messages([%{role: "user", content: "How did I drive?"}])
    end

    test "needs a question last and a non-empty transcript" do
      assert {:error, :empty_question} = Agent.validate_messages([])

      assert {:error, :invalid_messages} =
               Agent.validate_messages([%{role: "assistant", content: "hi"}])

      assert {:error, :invalid_messages} =
               Agent.validate_messages([
                 %{role: "user", content: "q"},
                 %{role: "assistant", content: "a"}
               ])
    end

    test "refuses a blank or oversized question" do
      assert {:error, :empty_question} =
               Agent.validate_messages([%{role: "user", content: "   "}])

      assert {:error, :question_too_long} =
               Agent.validate_messages([%{role: "user", content: String.duplicate("a", 1001)}])
    end

    test "bounds the whole transcript, not only each turn" do
      messages =
        for index <- 1..11 do
          role = if rem(index, 2) == 1, do: "user", else: "assistant"
          %{role: role, content: "turn #{index}"}
        end

      assert {:error, :conversation_too_long} = Agent.validate_messages(messages)
    end
  end

  describe "ask/2" do
    test "refuses when the assistant is switched off" do
      assert {:error, :disabled} = Agent.ask([%{role: "user", content: "hi"}])
    end

    test "refuses when no key is configured" do
      {:ok, _} = Agent.update_settings(%{"enabled" => true, "api_key" => nil})
      assert {:error, :not_configured} = Agent.ask([%{role: "user", content: "hi"}])
    end

    test "answers, streams events, and counts the question before the provider runs" do
      {:ok, _} =
        Agent.update_settings(%{"api_key" => "sk-test", "enabled" => true, "daily_limit" => 5})

      assert Agent.used_today() == 0

      assert {:ok, pid} =
               Agent.ask([%{role: "user", content: "How did I drive?"}],
                 snapshot: AgentSnapshotFixture.snapshot()
               )

      assert is_pid(pid)

      assert_receive {:agent_event, {:progress, _}}, 2000
      assert_receive {:agent_event, {:answer, answer}}, 2000
      assert answer =~ "150 km"
      assert_receive {:agent_event, {:meta, %{tokens: 7}}}, 2000
      assert_receive {:agent_event, :done}, 2000

      assert Agent.used_today() == 1
    end

    test "refuses once the daily allowance is spent" do
      {:ok, _} =
        Agent.update_settings(%{"api_key" => "sk-test", "enabled" => true, "daily_limit" => 1})

      assert {:ok, _pid} =
               Agent.ask([%{role: "user", content: "first"}],
                 snapshot: AgentSnapshotFixture.snapshot()
               )

      assert {:error, :daily_limit} =
               Agent.ask([%{role: "user", content: "second"}],
                 snapshot: AgentSnapshotFixture.snapshot()
               )
    end
  end

  describe "error_message/1" do
    test "maps every reason to a sentence" do
      for reason <- [
            :disabled,
            :not_configured,
            :daily_limit,
            :question_too_long,
            :conversation_too_long
          ] do
        message = Agent.error_message(reason)
        assert String.length(message) > 20
      end
    end
  end
end
