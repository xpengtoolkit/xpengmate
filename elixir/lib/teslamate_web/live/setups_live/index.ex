defmodule TeslaMateWeb.SetupsLive.Index do
  @moduledoc """
  Which tyres, trailer, roof rack or load each car had over which dates, and how
  the energy consumption compares between those setups.

  A setup is one aspect over an inclusive date range, and the aspects are
  independent, so rows are grouped by car and aspect and their ranges may
  overlap. `TeslaMate.Setups.comparison/0` attributes each drive to the newest
  setup of an aspect that covers its local calendar day; nothing is written onto
  the drive, so fixing a date here re-tags history.
  """

  use TeslaMateWeb, :live_view

  alias TeslaMate.{Convert, Setups}
  alias TeslaMate.Settings.GlobalSettings
  alias TeslaMateWeb.CarTitle

  @km_per_mile 1.609344

  on_mount {TeslaMateWeb.InitAssigns, :locale}

  @impl true
  def mount(_params, %{"settings" => settings}, socket) do
    {:ok, socket |> assign(:settings, settings) |> load_data()}
  end

  @impl true
  def handle_event("delete", %{"id" => id}, socket) do
    {:ok, _setup} = id |> Setups.get_setup!() |> Setups.delete_setup()

    {:noreply, load_data(socket)}
  end

  # Private

  defp load_data(%{assigns: %{settings: %GlobalSettings{} = settings}} = socket) do
    setups = Setups.list_setups()

    assign(socket,
      page_title: gettext("Setups"),
      cars: Setups.list_cars(),
      setup_groups: group_setups(setups),
      drive_counts: Setups.drive_counts(),
      overlapping_ids: overlapping_ids(setups),
      comparison_groups: comparison_groups(),
      timezone: Setups.timezone(),
      unit_of_length: settings.unit_of_length,
      unit_of_temperature: settings.unit_of_temperature,
      grafana_url: settings.grafana_url
    )
  end

  defp group_setups([]), do: []

  defp group_setups(setups) do
    setups
    |> Enum.group_by(& &1.car_id)
    |> Enum.map(fn {_car_id, car_setups} ->
      %{car: hd(car_setups).car, aspects: aspect_groups(car_setups)}
    end)
    |> Enum.sort_by(& &1.car.id)
  end

  defp aspect_groups(car_setups) do
    for aspect <- Setups.aspects(),
        rows = Enum.filter(car_setups, &(&1.aspect == aspect)),
        rows != [] do
      %{aspect: aspect, setups: rows}
    end
  end

  # Two rows of the same car and aspect overlap when their inclusive ranges
  # intersect. A missing end date is still "fitted", so it reaches to infinity.
  defp overlapping_ids(setups) do
    setups
    |> Enum.group_by(&{&1.car_id, &1.aspect})
    |> Enum.flat_map(fn {_key, rows} ->
      for a <- rows, b <- rows, a.id != b.id, ranges_overlap?(a, b), do: a.id
    end)
    |> MapSet.new()
  end

  defp ranges_overlap?(a, b) do
    a_end = a.ended_at || ~D[9999-12-31]
    b_end = b.ended_at || ~D[9999-12-31]

    Date.compare(a.started_at, b_end) != :gt and Date.compare(b.started_at, a_end) != :gt
  end

  # Group `Setups.comparison/0` by car and aspect, best consumption first so the
  # winner of each aspect is at the top. Rows without a consumption (no energy
  # estimate) sort last.
  defp comparison_groups do
    Setups.comparison()
    |> Enum.group_by(&{&1.car_id, &1.car_name, &1.aspect})
    |> Enum.map(fn {{car_id, car_name, aspect}, rows} ->
      %{
        car_id: car_id,
        car_name: car_name,
        aspect: aspect,
        rows: Enum.sort_by(rows, &comparison_sort_key/1)
      }
    end)
    |> Enum.sort_by(fn group -> {group.car_id, aspect_index(group.aspect)} end)
  end

  defp comparison_sort_key(%{consumption_wh_km: nil}), do: {1, 0.0}
  defp comparison_sort_key(%{consumption_wh_km: wh_km}), do: {0, wh_km}

  defp aspect_index(aspect) do
    Enum.find_index(Setups.aspects(), &(&1 == aspect)) || length(Setups.aspects())
  end

  defp aspect_label(:tyres), do: gettext("Tyres")
  defp aspect_label(:towing), do: gettext("Towing")
  defp aspect_label(:roof_rack), do: gettext("Roof rack")
  defp aspect_label(:loaded), do: gettext("Loaded")
  defp aspect_label(:other), do: gettext("Other")

  defp format_date(nil), do: "—"
  defp format_date(%Date{} = date), do: Calendar.strftime(date, "%Y-%m-%d")

  defp format_until(nil), do: gettext("Current")
  defp format_until(%Date{} = date), do: Calendar.strftime(date, "%Y-%m-%d")

  defp format_distance(nil, _unit), do: "—"

  defp format_distance(km, :mi) when is_number(km) do
    "#{format_number(Convert.km_to_miles(km, 1), 1)} mi"
  end

  defp format_distance(km, :km) when is_number(km) do
    "#{format_number(km, 1)} km"
  end

  defp format_consumption(nil, _unit), do: "—"

  defp format_consumption(wh_km, :mi) when is_number(wh_km) do
    "#{format_number(wh_km * @km_per_mile, 1)} Wh/mi"
  end

  defp format_consumption(wh_km, :km) when is_number(wh_km) do
    "#{format_number(wh_km, 1)} Wh/km"
  end

  defp format_temperature(nil, _unit), do: "—"

  defp format_temperature(celsius, :F) when is_number(celsius) do
    "#{format_number(Convert.celsius_to_fahrenheit(celsius, 1), 1)} °F"
  end

  defp format_temperature(celsius, :C) when is_number(celsius) do
    "#{format_number(celsius, 1)} °C"
  end

  defp format_speed(nil, _unit), do: "—"

  defp format_speed(kmh, :mi) when is_number(kmh) do
    "#{format_number(Convert.km_to_miles(kmh, 1), 1)} mph"
  end

  defp format_speed(kmh, :km) when is_number(kmh) do
    "#{format_number(kmh, 1)} km/h"
  end

  defp format_number(value, precision) when is_number(value) do
    value
    |> Kernel.*(1.0)
    |> Float.round(precision)
    |> :erlang.float_to_binary(decimals: precision)
  end
end
