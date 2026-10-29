defmodule TeslaMateWeb.AgentMarkdownTest do
  use ExUnit.Case, async: true

  alias TeslaMateWeb.AgentMarkdown

  defp render(markdown) do
    markdown
    |> AgentMarkdown.render()
    |> Phoenix.HTML.safe_to_string()
  end

  describe "escaping" do
    test "model-provided HTML is shown as text, never interpreted" do
      html = render(~s{<script>alert("x")</script>})

      refute html =~ "<script>"
      assert html =~ "&lt;script&gt;"
      assert html =~ "&lt;/script&gt;"
    end

    test "returns safe HTML" do
      assert {:safe, _} = AgentMarkdown.render("hello")
    end

    test "escapes ampersands and quotes in plain text" do
      html = render(~s(Tom & Jerry said "hi" <b>))

      assert html =~ "&amp;"
      assert html =~ "&quot;"
      assert html =~ "&lt;b&gt;"
    end

    test "non-binary input renders as empty HTML" do
      assert render(nil) == ""
      assert render(42) == ""
    end
  end

  describe "blocks" do
    test "paragraphs" do
      assert render("one\n\ntwo") == "<p>one</p><p>two</p>"
    end

    test "headings" do
      assert render("## Summary") == "<h2>Summary</h2>"
      assert render("### Details") == "<h3>Details</h3>"
    end

    test "unordered lists" do
      assert render("- one\n- two\n* three") == "<ul><li>one</li><li>two</li><li>three</li></ul>"
    end

    test "ordered lists" do
      assert render("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"
    end

    test "fenced code blocks escape their content" do
      html = render("```\n<b>x</b>\n```")

      assert html == "<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>"
    end

    test "an unterminated fence renders to the end" do
      assert render("```\nline") == "<pre><code>line</code></pre>"
    end
  end

  describe "inline" do
    test "bold, italic and inline code" do
      assert render("**bold**") == "<p><strong>bold</strong></p>"
      assert render("*italic*") == "<p><em>italic</em></p>"
      assert render("`code`") == "<p><code>code</code></p>"
    end

    test "inline code is escaped" do
      assert render("`<b>`") == "<p><code>&lt;b&gt;</code></p>"
    end

    test "http and https links become anchors" do
      html = render("[site](https://example.com/page)")

      assert html =~ ~s(href="https://example.com/page")
      assert html =~ ~s(target="_blank")
      assert html =~ ~s(rel="noopener")
      assert html =~ ">site</a>"
    end

    test "mailto links become anchors" do
      assert render("[mail](mailto:owner@example.com)") =~ ~s(href="mailto:owner@example.com")
    end

    test "non-http links are shown as text" do
      html = render("[click](ftp://example.com/x)")

      refute html =~ "<a"
      assert html =~ "ftp://example.com/x"
    end

    test "javascript links are shown as text" do
      html = render("[click](javascript:alert)")

      refute html =~ "<a"
      assert html =~ "javascript:alert"
    end
  end
end
