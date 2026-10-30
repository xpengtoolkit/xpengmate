defmodule TeslaMateWeb.PenguTest do
  use ExUnit.Case, async: true

  import Phoenix.LiveViewTest

  alias TeslaMateWeb.Pengu

  defp draw(assigns), do: render_component(&Pengu.pengu/1, assigns)

  describe "generated artwork (art: true)" do
    test "stacks one transparent image per mood behind the state hook" do
      html = draw(%{state: :idle, art: true})

      assert html =~ ~s(id="pengu")
      assert html =~ "pengu--art"
      assert html =~ ~s(data-state="idle")

      for file <- ~w(idle listening thinking speaking error) do
        assert html =~ ~s(/images/pengu/#{file}.png)
        assert html =~ "pengu-art--#{file}"
      end
    end

    test "keeps a still frame for readers who ask for reduced motion" do
      html = draw(%{state: :thinking, art: true})
      assert html =~ "/images/pengu/idle-still.png"
      assert html =~ "pengu-art--still"
    end

    test "draws no SVG when the artwork is available" do
      refute draw(%{state: :idle, art: true}) =~ "pengu-visor"
    end

    test "the bubble still says what the assistant is doing" do
      assert draw(%{state: :listening, art: true}) =~ "Listening"
      assert draw(%{state: :thinking, art: true, note: "read 379 drives"}) =~ "read 379 drives"
      assert draw(%{state: :error, art: true}) =~ "try again"
    end

    test "idle is quiet unless it is the first visit" do
      refute draw(%{state: :idle, art: true}) =~ "pengu-bubble"
      assert draw(%{state: :idle, art: true, hint: true}) =~ "ask me about your car"
    end

    test "the speaking copy does not claim the answer is below" do
      html = draw(%{state: :speaking, art: true})

      assert html =~ "Here"
      refute html =~ "below"
      refute html =~ "above"
    end

    test "a note is escaped, never treated as markup" do
      html = draw(%{state: :thinking, art: true, note: "<script>alert(1)</script>"})
      refute html =~ "<script>"
      assert html =~ "&lt;script&gt;"
    end
  end

  describe "drawn SVG fallback (art: false)" do
    test "draws the character with no asset dependency" do
      html = draw(%{state: :idle, art: false})

      assert html =~ "pengu-bot"
      assert html =~ "pengu-visor"
      refute html =~ "pengu-art"
    end

    test "every mood has a hook for the stylesheet" do
      for state <- [:idle, :listening, :thinking, :speaking, :error] do
        assert draw(%{state: state, art: false}) =~ ~s(data-state="#{state}")
      end
    end

    test "the bubble and the busy dots are the same as in art mode" do
      html = draw(%{state: :thinking, art: false, note: "read 379 drives"})
      assert html =~ "read 379 drives"
      assert html =~ "pengu-bubble-dots"
      refute draw(%{state: :speaking, art: false}) =~ "pengu-bubble-dots"
    end
  end

  test "the wrapper is decorative for assistive tech in both renderings" do
    assert draw(%{state: :thinking, art: true}) =~ ~s(aria-hidden="true")
    assert draw(%{state: :thinking, art: false}) =~ ~s(aria-hidden="true")
  end

  test "art?/0 reports what the build carries" do
    # The artwork is committed under priv/static/images/pengu, so a checkout
    # that has it says so; the page then renders it instead of the SVG.
    assert is_boolean(Pengu.art?())
  end
end
