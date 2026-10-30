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

  def pengu(assigns) do
    assigns =
      assigns
      |> assign(:bubble, bubble(assigns.state, assigns.note, assigns.hint))
      |> assign(:show_bubble, assigns.state != :idle or assigns.hint)
      |> assign(:busy?, assigns.state in [:listening, :thinking])

    ~H"""
    <div
      id="pengu"
      class="pengu"
      data-state={@state}
      data-hint={to_string(@hint)}
      aria-hidden="true"
    >
      <div class="pengu-stage">
        <svg class="pengu-bot" viewBox="0 0 120 132" role="presentation" focusable="false">
          <ellipse class="pengu-shadow" cx="60" cy="124" rx="26" ry="5" />

          <g class="pengu-float">
            <rect class="pengu-arm" x="22" y="76" width="12" height="22" rx="6" />
            <rect class="pengu-arm" x="86" y="76" width="12" height="22" rx="6" />
            <rect class="pengu-torso" x="36" y="70" width="48" height="38" rx="18" />
            <circle class="pengu-core" cx="60" cy="89" r="6" />
            <circle class="pengu-core-dot" cx="60" cy="89" r="2.5" />

            <g class="pengu-head">
              <line class="pengu-antenna" x1="60" y1="30" x2="60" y2="19" />
              <circle class="pengu-antenna-tip" cx="60" cy="15" r="4" />

              <rect class="pengu-ear" x="18" y="44" width="8" height="18" rx="4" />
              <rect class="pengu-ear" x="94" y="44" width="8" height="18" rx="4" />

              <rect class="pengu-shell" x="26" y="30" width="68" height="46" rx="23" />
              <rect class="pengu-visor" x="36" y="38" width="48" height="30" rx="15" />

              <g class="pengu-eyes">
                <rect class="pengu-eye" x="47" y="47" width="9" height="12" rx="4.5" />
                <rect class="pengu-eye" x="64" y="47" width="9" height="12" rx="4.5" />
              </g>

              <g class="pengu-wave">
                <rect class="pengu-wave-bar" x="50" y="61" width="3" height="6" rx="1.5" />
                <rect class="pengu-wave-bar" x="56.5" y="61" width="3" height="6" rx="1.5" />
                <rect class="pengu-wave-bar" x="63" y="61" width="3" height="6" rx="1.5" />
                <rect class="pengu-wave-bar" x="69.5" y="61" width="3" height="6" rx="1.5" />
              </g>
            </g>
          </g>

          <g class="pengu-ripples">
            <circle class="pengu-ripple" cx="60" cy="15" r="6" />
            <circle class="pengu-ripple pengu-ripple-2" cx="60" cy="15" r="6" />
          </g>

          <g class="pengu-orbit">
            <circle class="pengu-orbit-dot" cx="60" cy="5" r="2.6" />
            <circle class="pengu-orbit-dot" cx="70" cy="10" r="2.1" />
            <circle class="pengu-orbit-dot" cx="50" cy="10" r="2.1" />
          </g>
        </svg>
      </div>

      <div :if={@show_bubble} class="pengu-bubble">
        <span>{@bubble}</span>
        <span :if={@busy?} class="pengu-bubble-dots"><i></i><i></i><i></i></span>
      </div>
    </div>
    """
  end

  defp bubble(:idle, _note, true),
    do:
      gettext("Hey - ask me about your car. I can read your drives, charging, tyres and battery.")

  defp bubble(:idle, _note, _hint), do: ""

  defp bubble(:listening, _note, _hint), do: gettext("Listening…")

  defp bubble(:thinking, note, _hint) when is_binary(note) and note != "", do: note
  defp bubble(:thinking, _note, _hint), do: gettext("Checking your data…")

  defp bubble(:speaking, _note, _hint), do: gettext("Here's what I found below.")

  defp bubble(:error, _note, _hint), do: gettext("That didn't work. Want to try again?")
end
