defmodule TeslaMateWeb.AgentMarkdown do
  @moduledoc """
  A deliberately small Markdown renderer for the Ask assistant's answers.

  Everything the model writes is untrusted, so every piece of text is escaped
  with `Phoenix.HTML.html_escape/1`; the only HTML that reaches the page is the
  closed set of tags this module emits itself. There is no raw-HTML
  pass-through and no new dependency.

  Supported constructs:

    * paragraphs (blank-line separated)
    * `#`/`##`/`###` (and deeper) headings
    * unordered lists (`- ` / `* `)
    * ordered lists (`1. `)
    * bold (`**x**`), italic (`*x*`) and inline code (`` `x` ``)
    * fenced code blocks (` ``` `)
    * links (`[text](url)`), but only `http`, `https` and `mailto` URLs become
      anchors. Anything else is shown literally.

  Model-provided HTML is never interpreted: a `<script>` in an answer is
  rendered as the text `&lt;script&gt;`.
  """

  @type safe_html :: Phoenix.HTML.safe()

  # Order matters: code spans first, then links, bold, italic. Each pattern
  # stops at newlines, because blocks are parsed line by line.
  @inline_token ~r/`[^`\n]*`|\*\*[^\n]+?\*\*|\*[^*\n]+?\*|\[[^\[\]\n]*\]\([^()\s]*\)/u
  @link ~r/\A\[([^\[\]\n]*)\]\(([^()\s]*)\)\z/u
  @allowed_schemes ~w(http https mailto)
  @heading ~r/\A(\#{1,6})\s+(.*)\z/u
  @ul_item ~r/\A\s*[-*]\s+(.*)\z/u
  @ol_item ~r/\A\s*\d+[.)]\s+(.*)\z/u

  @doc """
  Renders `markdown` to safe HTML. Non-binary input renders as empty HTML.
  """
  @spec render(term()) :: safe_html()
  def render(markdown) when is_binary(markdown) do
    html =
      markdown
      |> String.replace("\r\n", "\n")
      |> String.replace("\r", "\n")
      |> String.split("\n")
      |> blocks()

    Phoenix.HTML.raw(html)
  end

  def render(_markdown), do: Phoenix.HTML.raw("")

  # -- blocks ---------------------------------------------------------------

  defp blocks(lines), do: do_blocks(lines, [])

  defp do_blocks([], acc), do: Enum.reverse(acc)

  defp do_blocks([line | rest], acc) do
    cond do
      blank?(line) ->
        do_blocks(rest, acc)

      fence?(line) ->
        {code, rest} = take_code(rest, [])
        do_blocks(rest, [pre(code) | acc])

      heading = heading(line) ->
        {level, text} = heading

        do_blocks(rest, [
          ["<h", to_string(level), ">", inline(text), "</h", to_string(level), ">"] | acc
        ])

      ul_item(line) ->
        {items, rest} = take_list(rest, [line], &ul_item/1)
        do_blocks(rest, [unordered_list(items) | acc])

      ol_item(line) ->
        {items, rest} = take_list(rest, [line], &ol_item/1)
        do_blocks(rest, [ordered_list(items) | acc])

      true ->
        {lines, rest} = take_paragraph(rest, [line])
        do_blocks(rest, [paragraph(lines) | acc])
    end
  end

  defp blank?(line), do: String.trim(line) == ""

  defp fence?(line), do: String.starts_with?(String.trim_leading(line), "```")

  defp heading(line) do
    case Regex.run(@heading, line) do
      [_, hashes, text] -> {String.length(hashes), text}
      _ -> nil
    end
  end

  defp ul_item(line) do
    case Regex.run(@ul_item, line) do
      [_, text] -> text
      _ -> nil
    end
  end

  defp ol_item(line) do
    case Regex.run(@ol_item, line) do
      [_, text] -> text
      _ -> nil
    end
  end

  defp take_code([], acc), do: {Enum.join(Enum.reverse(acc), "\n"), []}

  defp take_code([line | rest], acc) do
    if fence?(line) do
      {Enum.join(Enum.reverse(acc), "\n"), rest}
    else
      take_code(rest, [line | acc])
    end
  end

  defp take_list([], acc, _matcher), do: {Enum.reverse(acc), []}

  defp take_list([line | rest], acc, matcher) do
    case matcher.(line) do
      nil -> {Enum.reverse(acc), [line | rest]}
      _ -> take_list(rest, [line | acc], matcher)
    end
  end

  defp take_paragraph([], acc), do: {Enum.reverse(acc), []}

  defp take_paragraph([line | rest] = lines, acc) do
    if block_start?(line) do
      {Enum.reverse(acc), lines}
    else
      take_paragraph(rest, [line | acc])
    end
  end

  defp block_start?(line) do
    blank?(line) or fence?(line) or heading(line) != nil or ul_item(line) != nil or
      ol_item(line) != nil
  end

  defp pre(code), do: ["<pre><code>", escape(code), "</code></pre>"]

  defp paragraph(lines), do: ["<p>", inline(Enum.join(lines, " ")), "</p>"]

  defp unordered_list(items) do
    ["<ul>", Enum.map(items, fn line -> ["<li>", inline(ul_item(line)), "</li>"] end), "</ul>"]
  end

  defp ordered_list(items) do
    ["<ol>", Enum.map(items, fn line -> ["<li>", inline(ol_item(line)), "</li>"] end), "</ol>"]
  end

  # -- inline ---------------------------------------------------------------

  defp inline(""), do: []

  defp inline(text) do
    case Regex.run(@inline_token, text, return: :index) do
      [{start, length}] ->
        before = binary_part(text, 0, start)
        token = binary_part(text, start, length)
        rest = binary_part(text, start + length, byte_size(text) - start - length)

        [escape(before), inline_token(token), inline(rest)]

      nil ->
        escape(text)
    end
  end

  defp inline_token("`" <> _ = token) do
    content =
      token
      |> String.trim_leading("`")
      |> String.trim_trailing("`")

    ["<code>", escape(content), "</code>"]
  end

  defp inline_token("**" <> _ = token) do
    content =
      token
      |> String.replace_prefix("**", "")
      |> String.replace_suffix("**", "")

    ["<strong>", inline(content), "</strong>"]
  end

  defp inline_token("*" <> _ = token) do
    content =
      token
      |> String.replace_prefix("*", "")
      |> String.replace_suffix("*", "")

    ["<em>", inline(content), "</em>"]
  end

  defp inline_token("[" <> _ = token) do
    case Regex.run(@link, token) do
      [_, label, url] ->
        if allowed_url?(url) do
          [
            "<a href=\"",
            escape(url),
            "\" target=\"_blank\" rel=\"noopener\">",
            inline(label),
            "</a>"
          ]
        else
          escape(token)
        end

      _ ->
        escape(token)
    end
  end

  defp inline_token(token), do: escape(token)

  defp allowed_url?(url) do
    case URI.parse(url) do
      %URI{scheme: scheme} when is_binary(scheme) ->
        String.downcase(scheme) in @allowed_schemes

      _ ->
        false
    end
  end

  # -- escaping -------------------------------------------------------------

  defp escape(text) do
    text
    |> Phoenix.HTML.html_escape()
    |> Phoenix.HTML.safe_to_string()
  end
end
