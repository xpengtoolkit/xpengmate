defmodule TeslaMate.Agent.Chart do
  @moduledoc """
  The one chart an answer may carry.

  The model does not describe a chart and never writes markup: it names one of
  four charts from a fixed catalogue, and this module computes the series from
  the same snapshot the tools read and draws a self-contained SVG. A model that
  asks for an unknown chart, or a chart that has no data in the window, gets an
  error it can correct - it cannot get a chart it did not earn.

  The SVG is built from our own strings and numbers only; every label and title
  is XML-escaped, so nothing the model writes can become markup.
  """

  alias TeslaMate.Agent.Value

  @charts ~w(monthly_consumption monthly_distance setup_comparison charging_energy)
  @kinds ~w(bar line)
  @max_months 24
  @max_points 12
  @aspects ~w(tyres towing roof_rack loaded other)

  @doc "The catalogue, as prose for the system prompt and the tool description."
  def catalogue do
    """
    - monthly_consumption (bar or line): consumption in kWh/100 km for each of the last up to 24 months.
    - monthly_distance (bar or line): distance driven in km for each of the last up to 24 months.
    - setup_comparison (bar only): consumption per recorded setup label for one aspect (tyres by default), including the "Not recorded" baseline.
    - charging_energy (bar or line): energy added while charging in kWh per month.
    A chart is drawn under the answer, so refer to it as "the chart below". Do not describe its numbers as if they came from the chart; quote the tool figures in the text.
    """
  end

  def chart_ids, do: @charts
  def kinds, do: @kinds

  @doc """
  Validates a proposal and renders it.

  Returns `{:ok, %{id:, kind:, title:, svg:}}` or `{:error, message}`.
  """
  def build(snapshot, args) when is_map(args) do
    id = Map.get(args, "chart")
    kind = Map.get(args, "kind", "bar")
    aspect = Map.get(args, "aspect", "tyres")

    cond do
      id not in @charts ->
        {:error, "Unknown chart. Choose one of: #{Enum.join(@charts, ", ")}."}

      kind not in @kinds ->
        {:error, "Unknown chart kind. Use bar or line."}

      id == "setup_comparison" and aspect not in @aspects ->
        {:error, "setup_comparison needs an aspect: #{Enum.join(@aspects, ", ")}."}

      id == "setup_comparison" and kind != "bar" ->
        {:error,
         "setup_comparison is a bar chart; comparing categories on a line would imply a trend."}

      id == "setup_comparison" ->
        with {:ok, points} <- setup_points(snapshot, aspect) do
          render(
            snapshot,
            id,
            kind,
            title(args, "Consumption by #{String.replace(aspect, "_", " ")}"),
            points
          )
        end

      id == "charging_energy" ->
        with {:ok, points} <- monthly_charge_points(snapshot) do
          render(snapshot, id, kind, title(args, "Energy added per month"), points)
        end

      true ->
        with {:ok, points} <- monthly_drive_points(snapshot) do
          points =
            if id == "monthly_consumption" do
              Enum.map(points, fn {label, _distance, energy, distance} ->
                consumption = if distance > 0, do: Value.round(energy / distance * 100, 1)
                {label, consumption}
              end)
            else
              Enum.map(points, fn {label, distance, _energy, _d} -> {label, distance} end)
            end

          default_title =
            if id == "monthly_consumption",
              do: "Consumption per month",
              else: "Distance per month"

          render(snapshot, id, kind, title(args, default_title), points)
        end
    end
  end

  def build(_snapshot, _args), do: {:error, "The chart request was not readable."}

  defp title(args, default) do
    case Map.get(args, "title") do
      title when is_binary(title) ->
        title
        |> String.replace(~r/\s+/, " ")
        |> String.trim()
        |> String.slice(0, 80)
        |> case do
          "" -> default
          trimmed -> trimmed
        end

      _ ->
        default
    end
  end

  defp monthly_drive_points(snapshot) do
    points =
      snapshot.drives
      |> Enum.reject(&is_nil(&1.started_at))
      |> Enum.group_by(&month(&1.started_at, snapshot.tz))
      |> Enum.sort_by(fn {month, _drives} -> month end, Date)
      |> Enum.take(-@max_months)
      |> Enum.filter(fn {_month, drives} -> drives != [] end)
      |> Enum.map(fn {month, drives} ->
        distance = Value.sum(drives |> Enum.map(& &1.distance_km) |> Enum.reject(&is_nil/1))
        energy = Value.sum(drives |> Enum.map(& &1.energy_kwh) |> Enum.reject(&is_nil/1))
        {month_label(month), Value.round(distance, 1), energy, distance}
      end)

    if points == [],
      do: {:error, "There is no driving data in the window to chart."},
      else: {:ok, points}
  end

  defp monthly_charge_points(snapshot) do
    points =
      snapshot.charges
      |> Enum.reject(&is_nil(&1.started_at))
      |> Enum.group_by(&month(&1.started_at, snapshot.tz))
      |> Enum.sort_by(fn {month, _charges} -> month end, Date)
      |> Enum.take(-@max_months)
      |> Enum.map(fn {month, charges} ->
        added = Value.sum(charges |> Enum.map(& &1.energy_added_kwh) |> Enum.reject(&is_nil/1))
        {month_label(month), Value.round(added, 1)}
      end)
      |> Enum.filter(fn {_label, value} -> value > 0 end)

    if points == [],
      do: {:error, "There is no charging data in the window to chart."},
      else: {:ok, points}
  end

  defp setup_points(snapshot, aspect) do
    aspect_atom = String.to_existing_atom(aspect)

    points =
      snapshot.drives
      |> Enum.reject(&is_nil(&1.started_at))
      |> Enum.group_by(fn drive -> Map.get(drive.aspects, aspect_atom) end)
      |> Enum.sort_by(fn {label, _drives} -> {is_nil(label), label || ""} end)
      |> Enum.map(fn {label, drives} ->
        distance = Value.sum(drives |> Enum.map(& &1.distance_km) |> Enum.reject(&is_nil/1))
        energy = Value.sum(drives |> Enum.map(& &1.energy_kwh) |> Enum.reject(&is_nil/1))
        value = if distance > 0, do: Value.round(energy / distance * 100, 1)
        {label || "Not recorded", value}
      end)
      |> Enum.filter(fn {_label, value} -> is_number(value) end)
      |> Enum.take(@max_points)

    cond do
      points == [] ->
        {:error, "There is no #{aspect} data with a consumption figure to chart."}

      Enum.all?(points, fn {_label, value} -> value == 0 end) ->
        {:error, "The consumption figures for that aspect are all zero."}

      true ->
        {:ok, points}
    end
  end

  defp month(%DateTime{} = at, tz) do
    case DateTime.shift_zone(at, tz) do
      {:ok, local} -> Date.beginning_of_month(DateTime.to_date(local))
      _ -> Date.beginning_of_month(DateTime.to_date(at))
    end
  end

  defp month_label(%Date{} = month), do: Calendar.strftime(month, "%b %y")

  # -- SVG ------------------------------------------------------------------

  @width 720
  @height 260
  @pad_left 64
  @pad_right 16
  @pad_top 40
  @pad_bottom 56

  defp render(snapshot, id, kind, title, points) do
    values = Enum.map(points, fn {_label, value} -> value end)
    max_value = Enum.max(values, fn -> 0 end)
    max_value = if max_value > 0, do: max_value * 1.1, else: 1.0

    plot_w = @width - @pad_left - @pad_right
    plot_h = @height - @pad_top - @pad_bottom
    count = length(points)
    step = if count > 0, do: plot_w / count, else: plot_w

    y = fn value -> @pad_top + plot_h - value / max_value * plot_h end

    bars =
      if kind == "bar" do
        bar_w = max(step * 0.62, 4)

        points
        |> Enum.with_index()
        |> Enum.map(fn {{label, value}, index} ->
          x = @pad_left + step * index + (step - bar_w) / 2
          bar_h = max(@pad_top + plot_h - y.(value), 0)

          """
          <rect x="#{fmt(x)}" y="#{fmt(y.(value))}" width="#{fmt(bar_w)}" height="#{fmt(bar_h)}" rx="3" fill="#{bar_colour(index)}"><title>#{esc(label)}: #{fmt(value)}</title></rect>
          """
        end)
        |> Enum.join()
      else
        {line, dots} = line_and_dots(points, step, y)
        line <> dots
      end

    labels =
      points
      |> Enum.with_index()
      |> Enum.filter(fn {_point, index} -> show_label?(index, count) end)
      |> Enum.map(fn {{label, _value}, index} ->
        x = @pad_left + step * index + step / 2

        """
        <text x="#{fmt(x)}" y="#{@height - 26}" fill="#9fb0bd" font-size="11" text-anchor="end" transform="rotate(-30 #{fmt(x)} #{@height - 26})">#{esc(label)}</text>
        """
      end)
      |> Enum.join()

    grid =
      0..4
      |> Enum.map(fn index ->
        value = max_value / 4 * index
        y = y.(value)

        """
        <line x1="#{@pad_left}" y1="#{fmt(y)}" x2="#{@width - @pad_right}" y2="#{fmt(y)}" stroke="#2c3a44" stroke-width="1" stroke-dasharray="#{if index == 0, do: "0", else: "3 4"}"/>
        <text x="#{@pad_left - 8}" y="#{fmt(y + 4)}" fill="#9fb0bd" font-size="11" text-anchor="end">#{fmt(value)}</text>
        """
      end)
      |> Enum.join()

    unit = unit_for(id)

    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 #{@width} #{@height}" width="100%" role="img" aria-label="#{esc(title)}" style="max-width:#{@width}px;font-family:Inter,Helvetica,Arial,sans-serif;background:#0b1220;border-radius:10px">
    <text x="#{@pad_left}" y="22" fill="#dbe7ee" font-size="14" font-weight="600">#{esc(title)}</text>
    <text x="#{@width - @pad_right}" y="22" fill="#93a4b2" font-size="11" text-anchor="end">#{esc(unit)}</text>
    #{grid}
    #{bars}
    #{labels}
    </svg>
    """

    {:ok, %{id: id, kind: kind, title: title, svg: svg, points: points, period: snapshot.tz}}
  end

  defp line_and_dots(points, step, y) do
    indexed = Enum.with_index(points)

    coords =
      Enum.map(indexed, fn {{_label, value}, index} ->
        x = @pad_left + step * index + step / 2
        {x, y.(value)}
      end)

    polyline =
      coords
      |> Enum.map(fn {x, y} -> "#{fmt(x)},#{fmt(y)}" end)
      |> Enum.join(" ")

    dots =
      Enum.zip(indexed, coords)
      |> Enum.map(fn {{{label, value}, _index}, {x, y}} ->
        """
        <circle cx="#{fmt(x)}" cy="#{fmt(y)}" r="3.5" fill="#4cd7c4"><title>#{esc(label)}: #{fmt(value)}</title></circle>
        """
      end)
      |> Enum.join()

    line = """
    <polyline points="#{polyline}" fill="none" stroke="#4cd7c4" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
    """

    {line, dots}
  end

  defp show_label?(_index, count) when count <= 12, do: true
  defp show_label?(index, count), do: rem(index, div(count, 12) + 1) == 0

  defp bar_colour(index) do
    Enum.at(~w(#4cd7c4 #6ea8fe #f4b860 #c792ea #7ee787), rem(index, 5))
  end

  defp unit_for("monthly_consumption"), do: "kWh/100 km"
  defp unit_for("monthly_distance"), do: "km"
  defp unit_for("charging_energy"), do: "kWh"
  defp unit_for("setup_comparison"), do: "kWh/100 km"
  defp unit_for(_), do: ""

  defp fmt(value) when is_number(value), do: :erlang.float_to_binary(value * 1.0, decimals: 1)
  defp fmt(value), do: to_string(value)

  @doc false
  def esc(nil), do: ""

  def esc(value) when is_binary(value) do
    value
    |> String.replace("&", "&amp;")
    |> String.replace("<", "&lt;")
    |> String.replace(">", "&gt;")
    |> String.replace("\"", "&quot;")
    |> String.replace("'", "&#39;")
  end

  def esc(value), do: esc(to_string(value))
end
