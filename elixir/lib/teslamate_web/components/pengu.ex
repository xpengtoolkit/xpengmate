defmodule TeslaMateWeb.Pengu do
  @moduledoc """
  The Ask page's little assistant: an original inline-SVG robot whose moods are
  driven entirely by the LiveView.

  It is decoration with a job - it tells the reader, at a glance, whether the
  assistant is listening, thinking, talking or stuck - so the markup is one
  wrapper with `data-state`, and `assets/css/pengu.scss` turns each state into a
  CSS animation. No JavaScript hook, no image asset, nothing that can fail to
  load, and `prefers-reduced-motion` freezes it.

  The character is deliberately not the car's own mascot: same idea (a friendly
  robot that wakes when you talk to it), our own drawing.

  Two renderings share one state contract:

    * `art: true` (the default on the page) uses the generated artwork under
      `priv/static/images/pengu/` - a 3D-rendered robot with an animated APNG
      idle loop, produced by the OpenRouter pipeline in `scripts/pengu-art.py`.
      Each mood is a still frame; the idle mood is the loop.
    * `art: false` falls back to the hand-drawn SVG below, which needs no
      assets at all. `art?/0` says which one the build can serve.
  """

  use Phoenix.Component
  use Gettext, backend: TeslaMateWeb.Gettext

  @states [:idle, :listening, :thinking, :speaking, :error]

  attr :state, :atom,
    required: true,
    values: @states,
    doc: "idle, listening, thinking, speaking or error"

  attr :note, :string, default: nil, doc: "What the bubble shows while thinking"
  attr :hint, :boolean, default: false, doc: "Show the first-visit hint while idle"

  attr :art, :boolean,
    default: false,
    doc: "Render the generated artwork instead of the drawn SVG"

  def pengu(assigns) do
    assigns =
      assigns
      |> assign(:bubble, bubble(assigns.state, assigns.note, assigns.hint))
      |> assign(:show_bubble, assigns.state != :idle or assigns.hint)
      |> assign(:busy?, assigns.state in [:listening, :thinking])

    ~H"""
    <div
      id="pengu"
      class={"pengu" <> if(@art, do: " pengu--art", else: "")}
      data-state={@state}
      data-hint={to_string(@hint)}
      aria-hidden="true"
    >
      <div class="pengu-stage">
        <%= if @art do %>
          <img
            :for={{state, file} <- art_layers()}
            class={"pengu-art pengu-art--" <> to_string(state)}
            src={"/images/pengu/" <> file}
            alt=""
            draggable="false"
            decoding="async"
          />
          <%!-- Animated PNG cannot be frozen by CSS, so a reader who asks for
          reduced motion gets a still frame of the same character instead. --%>
          <img
            class="pengu-art pengu-art--still"
            src="/images/pengu/idle-still.png"
            alt=""
            draggable="false"
          />
        <% else %>
          <svg class="pengu-bot" viewBox="0 0 120 142" role="presentation" focusable="false">
            <defs>
              <linearGradient id="pengu-shell" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stop-color="#ffffff" />
                <stop offset="1" stop-color="#d9e4ee" />
              </linearGradient>
              <linearGradient id="pengu-visor-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stop-color="#101826" />
                <stop offset="1" stop-color="#05080e" />
              </linearGradient>
            </defs>

            <ellipse class="pengu-shadow" cx="60" cy="136" rx="30" ry="5" />

            <g class="pengu-float">
              <rect class="pengu-leg" x="44" y="118" width="13" height="14" rx="6.5" />
              <rect class="pengu-leg" x="62" y="118" width="13" height="14" rx="6.5" />
              <rect class="pengu-foot" x="41" y="129" width="17" height="8" rx="4" />
              <rect class="pengu-foot" x="61" y="129" width="17" height="8" rx="4" />
              <rect class="pengu-hips" x="42" y="111" width="36" height="11" rx="5.5" />

              <rect class="pengu-torso" x="32" y="80" width="56" height="36" rx="17" />
              <path class="pengu-mark" d="M56 96 l4 -4 l4 4 l-4 4 z" />
              <rect class="pengu-collar" x="44" y="75" width="32" height="7" rx="3.5" />

              <circle class="pengu-shoulder" cx="37" cy="93" r="6.5" />
              <line class="pengu-arm-outline" x1="37" y1="94" x2="28" y2="113" />
              <line class="pengu-arm" x1="37" y1="94" x2="28" y2="113" />
              <circle class="pengu-hand" cx="27" cy="116" r="7" />
              <circle class="pengu-thumb" cx="32" cy="120" r="2.6" />

              <circle class="pengu-shoulder" cx="83" cy="93" r="6.5" />
              <g class="pengu-arm-raised">
                <line class="pengu-arm-outline" x1="84" y1="93" x2="100" y2="55" />
                <line class="pengu-arm" x1="84" y1="93" x2="100" y2="55" />
                <circle class="pengu-hand" cx="102" cy="51" r="7.5" />
                <circle class="pengu-thumb" cx="97" cy="47" r="2.8" />
              </g>

              <g class="pengu-head">
                <rect class="pengu-pod" x="16" y="32" width="12" height="28" rx="6" />
                <rect class="pengu-pod" x="92" y="32" width="12" height="28" rx="6" />
                <circle class="pengu-pod-ring" cx="22" cy="46" r="2.6" />
                <circle class="pengu-pod-ring" cx="98" cy="46" r="2.6" />

                <rect class="pengu-helmet" x="24" y="12" width="72" height="62" rx="31" />
                <ellipse
                  class="pengu-gloss"
                  cx="43"
                  cy="25"
                  rx="12"
                  ry="6"
                  transform="rotate(-18 43 25)"
                />
                <rect class="pengu-visor" x="32" y="24" width="56" height="42" rx="21" />
                <ellipse class="pengu-visor-glow" cx="60" cy="59" rx="22" ry="5" />

                <g class="pengu-eyes">
                  <rect class="pengu-eye" x="43" y="37" width="14" height="19" rx="7" />
                  <rect class="pengu-eye" x="63" y="37" width="14" height="19" rx="7" />
                </g>
              </g>
            </g>

            <g class="pengu-ripples">
              <circle class="pengu-ripple" cx="60" cy="44" r="34" />
              <circle class="pengu-ripple pengu-ripple-2" cx="60" cy="44" r="34" />
            </g>

            <g class="pengu-orbit">
              <circle class="pengu-orbit-dot" cx="60" cy="-4" r="2.6" />
              <circle class="pengu-orbit-dot" cx="103" cy="19" r="2.2" />
              <circle class="pengu-orbit-dot" cx="17" cy="19" r="2.2" />
            </g>

            <g class="pengu-voice">
              <rect class="pengu-voice-bar" x="4" y="38" width="3" height="8" rx="1.5" />
              <rect class="pengu-voice-bar" x="-1" y="34" width="3" height="16" rx="1.5" />
              <rect class="pengu-voice-bar" x="9" y="40" width="3" height="6" rx="1.5" />
            </g>
          </svg>
        <% end %>
      </div>

      <div :if={@show_bubble} class="pengu-bubble">
        <span>{@bubble}</span>
        <span :if={@busy?} class="pengu-bubble-dots"><i></i><i></i><i></i></span>
      </div>
    </div>
    """
  end

  @art_dir "static/images/pengu"

  @doc """
  Whether this build carries the generated artwork.

  The page asks once per mount and falls back to the SVG without it, so a
  checkout that has never run the art pipeline still renders the mascot.
  """
  def art? do
    case :code.priv_dir(:teslamate) do
      path when is_list(path) -> File.dir?(Path.join(List.to_string(path), @art_dir))
      _ -> false
    end
  end

  defp art_layers do
    [
      {:idle, "idle.png"},
      {:listening, "listening.png"},
      {:thinking, "thinking.png"},
      {:speaking, "speaking.png"},
      {:error, "error.png"}
    ]
  end

  defp bubble(:idle, _note, true),
    do:
      gettext("Hey - ask me about your car. I can read your drives, charging, tyres and battery.")

  defp bubble(:idle, _note, _hint), do: ""

  defp bubble(:listening, _note, _hint), do: gettext("Listening…")

  defp bubble(:thinking, note, _hint) when is_binary(note) and note != "", do: note
  defp bubble(:thinking, _note, _hint), do: gettext("Checking your data…")

  # No "below"/"above": the bubble sits under the conversation, so the answer is
  # above it. Directional copy would be wrong the moment the layout changes.
  defp bubble(:speaking, _note, _hint), do: gettext("Here's what I found.")

  defp bubble(:error, _note, _hint), do: gettext("That didn't work. Want to try again?")
end
