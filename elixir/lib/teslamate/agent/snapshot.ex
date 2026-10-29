defmodule TeslaMate.Agent.Snapshot do
  @moduledoc """
  One bounded read of the car's data, taken once per question.

  The agent never queries the database itself: every tool is a pure function of
  this map. That is what makes a tool testable without a database or a model,
  and what makes it impossible for a model to reach a row that was not loaded
  here.

  The read is bounded on purpose, and says so: at most 2000 drives and 1000
  charging sessions over the last 730 days. `coverage.drives_truncated` is true
  when the caps dropped rows, so a tool can tell the reader that its window is
  the most recent N drives rather than the whole history.

  Energy per drive is TeslaMate's own estimate - the drop in the preferred
  range times the car's rated efficiency - the same basis the Efficiency
  dashboard and the Setups comparison use. It is `nil` when either side is
  missing; it is never assumed to be zero.

  Parked drain is measured **between two drives of the same car** when the gap
  is at least an hour: parked hours is the gap, and the energy lost is the
  rated-range drop across it. A gap with a charge in the middle is not a parked
  period, and the charge's own rows make it skip, which is why the figure is
  described as "between drives" rather than "total while parked".
  """

  import Ecto.Query, warn: false

  alias TeslaMate.{Repo, Setups, Settings}
  alias TeslaMate.Agent.Value
  alias TeslaMate.Import
  alias TeslaMate.Log.{Car, Charge, ChargingProcess, Drive, Position}

  @max_days 730
  @max_drives 2000
  @max_charges 1000
  @min_parked_minutes 60
  @pressure_window_days 30

  @doc """
  Loads the snapshot.

  Options: `:tz` (the deployment's zone, used to bucket calendar days) and
  `:today` (for tests).
  """
  def load(opts \\ []) do
    tz = Keyword.get_lazy(opts, :tz, &Setups.timezone/0)
    today = Keyword.get_lazy(opts, :today, &Date.utc_today/0)
    range = preferred_range()
    since = NaiveDateTime.utc_now() |> NaiveDateTime.add(-@max_days * 86_400, :second)

    cars = load_cars()
    names = car_names(cars)
    efficiencies = Map.new(cars, fn car -> {car.id, car.efficiency} end)
    drives = load_drives(names, efficiencies, since, range)
    charges = load_charges(names, since)
    setups = load_setups(names)

    %{
      now: DateTime.utc_now(),
      today: today,
      tz: tz,
      range: range,
      cars: cars,
      drives: attach_aspects(drives, setups, tz),
      charges: charges,
      setups: setups,
      parked: parked_periods(drives, cars),
      tyre_pressures: load_pressures(cars),
      battery: battery_estimates(charges),
      coverage: coverage(drives, charges)
    }
  end

  def max_days, do: @max_days

  # -- cars -----------------------------------------------------------------

  defp load_cars do
    from(c in Car,
      order_by: [asc: c.display_priority, asc: c.id],
      select: %{id: c.id, name: c.name, model: c.model, vin: c.vin, efficiency: c.efficiency}
    )
    |> Repo.all()
    |> Enum.map(fn car -> Map.update!(car, :efficiency, &Value.num/1) end)
  end

  defp car_names(cars), do: Map.new(cars, &{&1.id, car_name(&1)})

  defp car_name(%{name: name, vin: vin}) when name in [nil, ""], do: "VIN #{vin}"
  defp car_name(%{name: name}), do: name

  # -- drives ---------------------------------------------------------------

  defp load_drives(names, efficiencies, since, range) do
    from(d in Drive,
      where: d.start_date >= ^since,
      order_by: [desc: d.start_date],
      limit: @max_drives,
      preload: [:start_address, :end_address]
    )
    |> Repo.all()
    |> Enum.map(&drive(&1, names, Map.get(efficiencies, &1.car_id), range))
  end

  defp drive(drive, names, efficiency, range) do
    start_range = range_value(drive, range, :start)
    end_range = range_value(drive, range, :end)
    distance = Value.num(drive.distance)
    {energy, consumption} = energy_and_consumption(start_range, end_range, efficiency, distance)

    %{
      id: drive.id,
      car_id: drive.car_id,
      car_name: Map.get(names, drive.car_id, "Car #{drive.car_id}"),
      started_at: Value.utc(drive.start_date),
      ended_at: Value.utc(drive.end_date),
      distance_km: distance,
      duration_min: drive.duration_min,
      energy_kwh: Value.round(energy, 3),
      consumption_kwh_100km: Value.round(consumption, 2),
      outside_temp_c: Value.num(drive.outside_temp_avg),
      speed_max_kmh: drive.speed_max,
      ascent_m: drive.ascent,
      descent_m: drive.descent,
      start_address: address_name(drive.start_address),
      end_address: address_name(drive.end_address),
      start_range_km: start_range,
      end_range_km: end_range
    }
  end

  defp range_value(drive, "ideal", side),
    do: Value.num(Map.fetch!(drive, :"#{side}_ideal_range_km"))

  defp range_value(drive, _rated, side),
    do: Value.num(Map.fetch!(drive, :"#{side}_rated_range_km"))

  defp energy_and_consumption(start_range, end_range, efficiency, distance) do
    drop =
      if is_number(start_range) and is_number(end_range), do: start_range - end_range

    energy =
      if is_number(drop) and drop >= 0.1 and is_number(efficiency), do: drop * efficiency

    consumption =
      if is_number(energy) and is_number(distance) and distance > 0,
        do: energy / distance * 100

    {energy, consumption}
  end

  defp address_name(nil), do: nil
  defp address_name(%{display_name: name}) when is_binary(name) and name != "", do: name
  defp address_name(%{name: name}), do: name
  defp address_name(_), do: nil

  # -- setups and their attribution ----------------------------------------

  defp load_setups(names) do
    Setups.list_setups()
    |> Enum.map(fn setup ->
      %{
        id: setup.id,
        car_id: setup.car_id,
        car_name: Map.get(names, setup.car_id, "Car #{setup.car_id}"),
        aspect: setup.aspect,
        label: setup.label,
        started_at: setup.started_at,
        ended_at: setup.ended_at
      }
    end)
  end

  # Same rule as TeslaMate.Setups.comparison/0: the newest setup of an aspect
  # covering the drive's local calendar day. The number of setups is small, so
  # this runs in Elixir rather than a lateral join per drive.
  defp attach_aspects(drives, setups, tz) do
    by_car = Enum.group_by(setups, & &1.car_id)

    Enum.map(drives, fn drive ->
      Map.put(drive, :aspects, aspects_for(drive, Map.get(by_car, drive.car_id, []), tz))
    end)
  end

  defp aspects_for(drive, setups, tz) do
    day = local_day(drive.started_at, tz)

    setups
    |> Enum.filter(fn setup ->
      Date.compare(setup.started_at, day) != :gt and
        (is_nil(setup.ended_at) or Date.compare(day, setup.ended_at) != :gt)
    end)
    |> Enum.group_by(& &1.aspect)
    |> Enum.map(fn {aspect, rows} ->
      newest = Enum.max_by(rows, & &1.started_at, Date)
      {aspect, newest.label}
    end)
    |> Map.new()
  end

  defp local_day(%DateTime{} = at, tz) do
    case DateTime.shift_zone(at, tz) do
      {:ok, local} -> DateTime.to_date(local)
      _ -> DateTime.to_date(at)
    end
  end

  defp local_day(_at, _tz), do: Date.utc_today()

  # -- charging -------------------------------------------------------------

  defp load_charges(names, since) do
    processes =
      from(cp in ChargingProcess,
        where: cp.start_date >= ^since,
        order_by: [desc: cp.start_date],
        limit: @max_charges,
        preload: [:address]
      )
      |> Repo.all()

    peaks = peak_powers(Enum.map(processes, & &1.id))

    Enum.map(processes, fn cp ->
      %{
        id: cp.id,
        car_id: cp.car_id,
        car_name: Map.get(names, cp.car_id, "Car #{cp.car_id}"),
        started_at: Value.utc(cp.start_date),
        duration_min: cp.duration_min,
        energy_added_kwh: Value.num(cp.charge_energy_added),
        energy_used_kwh: Value.num(cp.charge_energy_used),
        start_soc: cp.start_battery_level,
        end_soc: cp.end_battery_level,
        cost: Value.num(cp.cost),
        location: address_name(cp.address),
        peak_power_kw: Map.get(peaks, cp.id)
      }
    end)
  end

  defp peak_powers([]), do: %{}

  defp peak_powers(ids) do
    from(c in Charge,
      where: c.charging_process_id in ^ids,
      group_by: c.charging_process_id,
      select: {c.charging_process_id, max(c.charger_power)}
    )
    |> Repo.all()
    |> Map.new(fn {id, power} -> {id, Value.num(power)} end)
  end

  # -- parked periods between drives ---------------------------------------

  defp parked_periods(drives, cars) do
    efficiencies = Map.new(cars, fn car -> {car.id, car.efficiency} end)

    drives
    |> Enum.reject(&is_nil(&1.started_at))
    |> Enum.group_by(& &1.car_id)
    |> Enum.flat_map(fn {car_id, car_drives} ->
      car_drives
      |> Enum.sort_by(& &1.started_at, DateTime)
      |> Enum.chunk_every(2, 1, :discard)
      |> Enum.map(fn [previous, next] ->
        parked_between(previous, next, Map.get(efficiencies, car_id))
      end)
      |> Enum.reject(&is_nil/1)
    end)
    |> Enum.sort_by(& &1.hours, :desc)
  end

  defp parked_between(previous, next, efficiency) do
    from = previous.ended_at || previous.started_at
    hours = if from && next.started_at, do: DateTime.diff(next.started_at, from, :second) / 3600

    with %DateTime{} <- from,
         true <- is_number(hours) and hours >= @min_parked_minutes / 60,
         lost <- parked_energy(previous.end_range_km, next.start_range_km, efficiency) do
      %{
        car_id: next.car_id,
        car_name: next.car_name,
        from: from,
        to: next.started_at,
        hours: Value.round(hours, 1),
        energy_lost_kwh: lost
      }
    else
      _ -> nil
    end
  end

  defp parked_energy(end_range, start_range, efficiency) do
    if is_number(end_range) and is_number(start_range) and is_number(efficiency) do
      drop = end_range - start_range
      if drop > 0.1, do: Value.round(drop * efficiency, 3)
    end
  end

  # -- tyre pressures -------------------------------------------------------

  defp load_pressures(cars) do
    since = NaiveDateTime.utc_now() |> NaiveDateTime.add(-@pressure_window_days * 86_400, :second)

    cars
    |> Enum.flat_map(fn car ->
      case latest_pressure(car.id) do
        nil ->
          []

        latest ->
          [
            %{
              car_id: car.id,
              car_name: car_name(car),
              recorded_at: Value.utc(latest.date),
              front_left_bar: Value.num(latest.fl),
              front_right_bar: Value.num(latest.fr),
              rear_left_bar: Value.num(latest.rl),
              rear_right_bar: Value.num(latest.rr),
              window: pressure_window(car.id, since)
            }
          ]
      end
    end)
  end

  defp latest_pressure(car_id) do
    from(p in Position,
      where: p.car_id == ^car_id and not is_nil(p.tpms_pressure_fl),
      order_by: [desc: p.date],
      limit: 1,
      select: %{
        date: p.date,
        fl: p.tpms_pressure_fl,
        fr: p.tpms_pressure_fr,
        rl: p.tpms_pressure_rl,
        rr: p.tpms_pressure_rr
      }
    )
    |> Repo.one()
  end

  defp pressure_window(car_id, since) do
    from(p in Position,
      where: p.car_id == ^car_id and p.date >= ^since and not is_nil(p.tpms_pressure_fl),
      select: %{
        min_fl: min(p.tpms_pressure_fl),
        max_fl: max(p.tpms_pressure_fl),
        min_fr: min(p.tpms_pressure_fr),
        max_fr: max(p.tpms_pressure_fr),
        min_rl: min(p.tpms_pressure_rl),
        max_rl: max(p.tpms_pressure_rl),
        min_rr: min(p.tpms_pressure_rr),
        max_rr: max(p.tpms_pressure_rr)
      }
    )
    |> Repo.one()
    |> case do
      nil -> nil
      row -> Map.new(row, fn {key, value} -> {key, Value.num(value)} end)
    end
  end

  # -- battery estimate from charge sessions --------------------------------

  defp battery_estimates(charges) do
    charges
    |> Enum.filter(&usable_for_capacity?/1)
    |> Enum.group_by(& &1.car_id)
    |> Enum.map(fn {car_id, sessions} ->
      energy = Value.sum(Enum.map(sessions, & &1.energy_added_kwh))
      soc = Value.sum(Enum.map(sessions, &(&1.end_soc - &1.start_soc)))

      %{
        car_id: car_id,
        car_name: sessions |> hd() |> Map.get(:car_name),
        capacity_kwh: if(soc > 0, do: Value.round(energy / soc * 100, 1)),
        samples: length(sessions)
      }
    end)
    |> Enum.sort_by(& &1.car_name)
  end

  defp usable_for_capacity?(charge) do
    is_number(charge.energy_added_kwh) and charge.energy_added_kwh > 0.01 and
      is_integer(charge.start_soc) and is_integer(charge.end_soc) and
      charge.end_soc > charge.start_soc and charge.end_soc <= 95 and
      is_integer(charge.duration_min) and charge.duration_min > 10
  end

  # -- coverage -------------------------------------------------------------

  defp coverage(drives, charges) do
    drive_totals =
      from(d in Drive,
        select: %{drives: count(d.id), first: min(d.start_date), last: max(d.start_date)}
      )
      |> Repo.one()

    charge_totals =
      from(cp in ChargingProcess,
        select: %{charges: count(cp.id), first: min(cp.start_date), last: max(cp.start_date)}
      )
      |> Repo.one()

    imports =
      from(r in Import.Run,
        order_by: [desc: r.inserted_at],
        limit: 5,
        select: %{
          source_key: r.source_key,
          status: r.status,
          inserted_at: r.inserted_at,
          date_limit: r.date_limit
        }
      )
      |> Repo.all()
      |> Enum.map(fn row ->
        %{
          source_key: row.source_key,
          status: to_string(row.status),
          ran_at: Value.utc(row.inserted_at),
          date_limit: Value.utc(row.date_limit)
        }
      end)

    %{
      drives_total: drive_totals.drives || 0,
      charges_total: charge_totals.charges || 0,
      first_drive_at: Value.utc(drive_totals.first),
      last_drive_at: Value.utc(drive_totals.last),
      first_charge_at: Value.utc(charge_totals.first),
      last_charge_at: Value.utc(charge_totals.last),
      drives_returned: length(drives),
      drives_truncated: (drive_totals.drives || 0) > length(drives),
      charges_returned: length(charges),
      charges_truncated: (charge_totals.charges || 0) > length(charges),
      window_days: @max_days,
      imports: imports
    }
  end

  # -- helpers --------------------------------------------------------------

  defp preferred_range do
    case Settings.get_global_settings!().preferred_range do
      :ideal -> "ideal"
      _ -> "rated"
    end
  end
end
