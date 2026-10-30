defmodule TeslaMateWeb.PenguTest do
  use ExUnit.Case, async: true

  import Phoenix.LiveViewTest

  alias TeslaMateWeb.Pengu

  defp draw(assigns), do: render_component(&Pengu.pengu/1, assigns)

  test "draws the robot and exposes its mood on the wrapper" do
    html = draw(%{state: :idle})

    assert html =~ ~s(id="pengu")
    assert html =~ ~s(data-state="idle")
    assert html =~ "pengu-bot"
    assert html =~ "pengu-visor"
    assert html =~ ~s(aria-hidden="true")
  end

  test "every mood the LiveView can ask for has a hook for the stylesheet" do
    for state <- [:idle, :listening, :thinking, :speaking, :error] do
      assert draw(%{state: state}) =~ ~s(data-state="#{state}")
    end
  end

  test "the bubble says what the assistant is doing" do
    assert draw(%{state: :listening}) =~ "Listening"
    assert draw(%{state: :speaking}) =~ "Here"
    assert draw(%{state: :error}) =~ "try again"
  end

  test "thinking shows the progress note it is given, and dots while busy" do
    html = draw(%{state: :thinking, note: "read 379 drives and 119 charges"})

    assert html =~ "read 379 drives and 119 charges"
    assert html =~ "pengu-bubble-dots"
    refute draw(%{state: :speaking}) =~ "pengu-bubble-dots"
  end

  test "idle is quiet unless it is the first visit" do
    quiet = draw(%{state: :idle})
    refute quiet =~ "pengu-bubble"

    hint = draw(%{state: :idle, hint: true})
    assert hint =~ "pengu-bubble"
    assert hint =~ "ask me about your car"
  end

  test "a note is escaped, never treated as markup" do
    html = draw(%{state: :thinking, note: "<script>alert(1)</script>"})

    refute html =~ "<script>"
    assert html =~ "&lt;script&gt;"
  end
end
