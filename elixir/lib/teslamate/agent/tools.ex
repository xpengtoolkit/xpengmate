defmodule TeslaMate.Agent.Tools do
  @moduledoc """
  The assistant's tools: pure functions over one `TeslaMate.Agent.Snapshot`.

  A tool never touches the database and never sees a row the snapshot did not
  load, so a model cannot reach data that belongs to another window of the
  history than the one the question was answered in - and a tool can be tested
  against a hand-written snapshot with no database and no model.

  Every result carries a `_summary` string: the one-line "checked: …" note the
  page shows the reader, and the only thing the model is told about what it
  just did. A result that cannot be computed returns a `note` explaining why
  rather than a substitute number.
  """

  alias TeslaMate.Agent.{Chart, Value}

  @day 86_400
  @max_days 730

  @aspects ["tyres", "towing", "roof_rack", "loaded", "other"]

  # -- schema ---------------------------------------------------------------

  def schemas do
    [
      definition(
        "overview",
        "Totals for a period: driving distance, energy and consumption, charging energy (drawn from the wall vs stored in the pack), and the energy lost while parked. Start here for broad questions.",
        %{"days" => days_schema()}
      ),
      definition(
        "driving_series",
        "Distance, energy and consumption bucketed by day, week or month. Use for trends and 'which month was best' questions.",
        %{
          "days" => days_schema(),
          "bucket" => %{"type" => "string", "enum" => ["day", "week", "month"]}
        }
      ),
      definition(
        "consumption_by_setup",
        "Consumption grouped by the recorded vehicle setup: tyres, towing a trailer, a roof rack, a load. Drives covered by no setup of an aspect appear as 'Not recorded', the baseline. Use for tyre/setup comparison questions.",
        %{
          "days" => days_schema(),
          "aspect" => %{
            "type" => "string",
            "enum" => @aspects,
            "description" => "Limit to one aspect; omit for all recorded aspects."
          }
        }
      ),
      definition(
        "charging",
        "Charging totals and individual sessions, optionally only AC or only DC. Distinguishes energy drawn from the wall (used) from energy stored in the pack (added).",
        %{
          "days" => days_schema(),
          "kind" => %{"type" => "string", "enum" => ["all", "AC", "DC"]},
          "limit" => %{"type" => "integer", "minimum" => 1, "maximum" => 25}
        }
      ),
      definition(
        "trips",
        "Individual drives, newest first or sorted by distance or consumption. Use for 'longest', 'worst' and 'most recent' questions.",
        %{
          "days" => days_schema(),
          "sort" => %{"type" => "string", "enum" => ["recent", "distance", "consumption"]},
          "limit" => %{"type" => "integer", "minimum" => 1, "maximum" => 25}
        }
      ),
      definition(
        "parked",
        "Energy lost while parked, measured between consecutive drives (a charge in the middle splits a gap), with the longest parked periods.",
        %{"days" => days_schema()}
      ),
      definition(
        "tyre_pressures",
        "Measured tyre pressures: the latest reading per wheel and the min/max of the last 30 days, in bar.",
        %{"car" => %{"type" => "string", "description" => "Optional car name or VIN suffix."}}
      ),
      definition(
        "battery_health",
        "A pack capacity estimate from charge sessions, the number of sessions behind it, and the observed consumption the estimate depends on.",
        %{}
      ),
      definition(
        "data_coverage",
        "What data exists and over what period: drives, charges, first and last dates, whether the window is truncated, and the last imports.",
        %{}
      ),
      definition(
        "propose_chart",
        "Propose one chart to draw under the answer. The server owns the data and the rendering; you choose a chart from the catalogue and optionally a title. A second chart in one answer is refused.",
        %{
          "chart" => %{
            "type" => "string",
            "enum" => [
              "monthly_consumption",
              "monthly_distance",
              "setup_comparison",
              "charging_energy"
            ]
          },
          "kind" => %{"type" => "string", "enum" => ["bar", "line"]},
          "title" => %{"type" => "string", "description" => "Short title, at most 80 characters."},
          "aspect" => %{
            "type" => "string",
            "enum" => @aspects,
            "description" => "For setup_comparison only; defaults to tyres."
          }
        }
      )
    ]
  end

  def names, do: Enum.map(schemas(), & &1["function"]["name"])

  defp definition(name, description, properties) do
    %{
      "type" => "function",
      "function" => %{
        "name" => name,
        "description" => description,
        "parameters" => %{
          "type" => "object",
          "properties" => properties,
          "additionalProperties" => false
        }
      }
    }
  end

  defp days_schema do
    %{
      "type" => "integer",
      "minimum" => 1,
      "maximum" => @max_days,
      "description" => "Period length in days, ending now."
    }
  end

  @doc """
  Runs one tool. Unknown tools and handler crashes become an `error` result the
  model can read and correct, never an exception that ends the answer.
  """
  def execute(snapshot, name, args) when is_binary(name) do
    args = if is_map(args), do: args, else: %{}

    case handler(name) do
      nil ->
        %{ok: false, error: "Unknown tool: #{name}"}

      handler ->
        try do
          handler.(snapshot, args)
        rescue
          error ->
            %{
              ok: false,
              error: "That tool could not read the data just now.",
              note: Exception.message(error)
            }
        end
    end
  end

  defp handler("overview"), do: &overview/2
  defp handler("driving_series"), do: &driving_series/2
  defp handler("consumption_by_setup"), do: &consumption_by_setup/2
  defp handler("charging"), do: &charging/2
  defp handler("trips"), do: &trips/2
  defp handler("parked"), do: &parked/2
  defp handler("tyre_pressures"), do: &tyre_pressures/2
  defp handler("battery_health"), do: &battery_health/2
  defp handler("data_coverage"), do: &data_coverage/2
  defp handler("propose_chart"), do: &propose_chart/2
  defp handler(_), do: nil

  # -- overview -------------------------------------------------------------

  defp overview(snapshot, args) do
    days = int_arg(args, "days", 90)
    {from, drives, charges} = window(snapshot, days)
    driving = drive_totals(drives)
    charging = charge_totals(charges)
    parked = parked_in(snapshot, from)

    %{
      ok: true,
      period: period(snapshot, from, days),
      driving: driving,
      charging: charging,
      parked: parked,
      battery: snapshot.battery,
      notes: window_notes(snapshot),
      _summary: "read #{length(drives)} drives and #{length(charges)} charges over #{days} days"
    }
  end

  # -- driving_series -------------------------------------------------------

  defp driving_series(snapshot, args) do
    days = int_arg(args, "days", 365)
    bucket = str_arg(args, "bucket", ["day", "week", "month"], "month")
    {from, drives, _charges} = window(snapshot, days)

    series =
      drives
      |> Enum.group_by(&bucket_start(&1.started_at, bucket, snapshot.tz))
      |> Enum.sort_by(fn {start, _rows} -> start end, Date)
      |> Enum.map(fn {start, rows} ->
        totals = drive_totals(rows)

        %{
          bucket: Date.to_iso8601(start),
          drives: totals.drives,
          distance_km: totals.distance_km,
          energy_kwh: totals.energy_kwh,
          consumption_kwh_100km: totals.consumption_kwh_100km
        }
      end)
      |> Enum.take(-120)

    %{
      ok: true,
      period: period(snapshot, from, days),
      bucket: bucket,
      points: series,
      notes:
        window_notes(snapshot) ++
          if(length(series) >= 120,
            do: ["The series is capped at the most recent 120 buckets."],
            else: []
          ),
      _summary: "read #{length(series)} #{bucket} buckets"
    }
  end

  defp bucket_start(%DateTime{} = at, bucket, tz) do
    date =
      case DateTime.shift_zone(at, tz) do
        {:ok, local} -> DateTime.to_date(local)
        _ -> DateTime.to_date(at)
      end

    case bucket do
      "day" -> date
      "week" -> Date.beginning_of_week(date, :monday)
      _ -> Date.beginning_of_month(date)
    end
  end

  # -- consumption_by_setup -------------------------------------------------

  defp consumption_by_setup(snapshot, args) do
    days = int_arg(args, "days", 365)
    only = str_arg(args, "aspect", @aspects, nil)
    {from, drives, _charges} = window(snapshot, days)

    aspects = if only, do: [only], else: Enum.filter(@aspects, &aspect_present?(snapshot, &1))

    rows =
      for aspect <- aspects,
          {label, bucket} <- setup_buckets(drives, aspect) do
        totals = drive_totals(bucket)

        %{
          aspect: aspect,
          label: label || "Not recorded",
          drives: totals.drives,
          distance_km: totals.distance_km,
          energy_kwh: totals.energy_kwh,
          consumption_kwh_100km: totals.consumption_kwh_100km
        }
      end
      |> Enum.sort_by(&{&1.aspect, sort_consumption(&1.consumption_kwh_100km)})

    %{
      ok: true,
      period: period(snapshot, from, days),
      rows: rows,
      setups:
        Enum.map(snapshot.setups, &Map.drop(&1, [:id])) |> Enum.sort_by(&{&1.car_name, &1.aspect}),
      notes: setup_notes(rows, snapshot) ++ window_notes(snapshot),
      _summary: "read #{length(drives)} drives against #{length(rows)} setup buckets"
    }
  end

  defp aspect_present?(snapshot, aspect) do
    Enum.any?(snapshot.setups, &(Atom.to_string(&1.aspect) == aspect))
  end

  defp setup_buckets(drives, aspect) do
    drives
    |> Enum.group_by(fn drive ->
      Map.get(drive.aspects, String.to_existing_atom(aspect))
    end)
  end

  defp sort_consumption(nil), do: 1.0e9
  defp sort_consumption(value), do: value

  defp setup_notes(rows, snapshot) do
    short =
      rows
      |> Enum.filter(fn row -> is_number(row.distance_km) and row.distance_km < 100 end)
      |> Enum.map(
        &"#{&1.aspect} \"#{&1.label}\" covers only #{fmt(&1.distance_km)} km - too short for a fair comparison"
      )

    overlaps =
      snapshot.setups
      |> Enum.group_by(&{&1.car_id, &1.aspect})
      |> Enum.flat_map(fn {{_car, aspect}, setups} ->
        setups
        |> Enum.sort_by(& &1.started_at, Date)
        |> Enum.chunk_every(2, 1, :discard)
        |> Enum.filter(fn [a, b] ->
          is_nil(a.ended_at) or Date.compare(b.started_at, a.ended_at) != :gt
        end)
        |> Enum.map(fn [a, b] ->
          "the #{aspect} setups \"#{a.label}\" and \"#{b.label}\" overlap; a drive is attributed to the newest start (#{b.label})"
        end)
      end)
      |> Enum.uniq()

    short ++ overlaps
  end

  # -- charging -------------------------------------------------------------

  defp charging(snapshot, args) do
    days = int_arg(args, "days", 90)
    kind = str_arg(args, "kind", ["all", "AC", "DC"], "all")
    limit = int_arg(args, "limit", 10)
    {from, _drives, charges} = window(snapshot, days)

    charges = Enum.filter(charges, &(kind == "all" or charge_kind(&1) == kind))
    totals = charge_totals(charges)

    sessions =
      charges
      |> Enum.sort_by(&(&1.energy_added_kwh || 0), :desc)
      |> Enum.take(limit)
      |> Enum.map(fn charge ->
        %{
          started_at: iso(charge.started_at),
          kind: charge_kind(charge),
          energy_added_kwh: Value.round(charge.energy_added_kwh, 2),
          energy_used_kwh: Value.round(charge.energy_used_kwh, 2),
          duration_min: charge.duration_min,
          start_soc: charge.start_soc,
          end_soc: charge.end_soc,
          peak_power_kw: Value.round(charge.peak_power_kw, 1),
          cost: Value.round(charge.cost, 2),
          location: charge.location
        }
      end)

    %{
      ok: true,
      period: period(snapshot, from, days),
      kind: kind,
      totals: totals,
      sessions: sessions,
      notes:
        [
          "Energy used is drawn from the wall; energy added is stored in the pack. They differ by charging losses.",
          "AC/DC is derived from the session's peak charger power: above 22 kW is DC, at or below is AC, unknown when no charge sample has a power."
        ] ++ window_notes(snapshot),
      _summary: "read #{length(charges)} #{kind} charging sessions"
    }
  end

  defp charge_kind(%{peak_power_kw: nil}), do: "unknown"
  defp charge_kind(%{peak_power_kw: power}) when is_number(power) and power > 22, do: "DC"
  defp charge_kind(%{peak_power_kw: power}) when is_number(power), do: "AC"
  defp charge_kind(_), do: "unknown"

  # -- trips ----------------------------------------------------------------

  defp trips(snapshot, args) do
    days = int_arg(args, "days", 90)
    sort = str_arg(args, "sort", ["recent", "distance", "consumption"], "recent")
    limit = int_arg(args, "limit", 10)
    {from, drives, _charges} = window(snapshot, days)

    sorted =
      case sort do
        "distance" -> Enum.sort_by(drives, &(&1.distance_km || 0), :desc)
        "consumption" -> Enum.sort_by(drives, &sort_consumption_desc(&1.consumption_kwh_100km))
        _ -> Enum.sort_by(drives, & &1.started_at, {:desc, DateTime})
      end

    %{
      ok: true,
      period: period(snapshot, from, days),
      sort: sort,
      drives:
        sorted
        |> Enum.take(limit)
        |> Enum.map(fn drive ->
          %{
            started_at: iso(drive.started_at),
            car: drive.car_name,
            from: drive.start_address,
            to: drive.end_address,
            distance_km: Value.round(drive.distance_km, 1),
            duration_min: drive.duration_min,
            consumption_kwh_100km: Value.round(drive.consumption_kwh_100km, 1),
            outside_temp_c: Value.round(drive.outside_temp_c, 1),
            setups: drive.aspects
          }
        end),
      notes: window_notes(snapshot),
      _summary: "read #{length(drives)} drives, shown by #{sort}"
    }
  end

  defp sort_consumption_desc(nil), do: -1.0e9
  defp sort_consumption_desc(value), do: -value

  # -- parked ---------------------------------------------------------------

  defp parked(snapshot, args) do
    days = int_arg(args, "days", 90)
    {from, _drives, _charges} = window(snapshot, days)
    periods = parked_in(snapshot, from)

    %{
      ok: true,
      period: period(snapshot, from, days),
      totals: %{
        periods: periods.count,
        hours: periods.hours,
        energy_lost_kwh: periods.energy_lost_kwh
      },
      longest:
        periods.longest
        |> Enum.take(10)
        |> Enum.map(fn period ->
          %{
            car: period.car_name,
            from: iso(period.from),
            to: iso(period.to),
            hours: period.hours,
            energy_lost_kwh: period.energy_lost_kwh
          }
        end),
      notes: [
        "Parked drain is measured between consecutive drives of a car, so a charge in the middle of a gap splits it and is not counted as parked time.",
        "The energy figure is the rated-range drop across the gap times the car's efficiency; it is nil when the range is missing on either side."
      ],
      _summary: "read #{periods.count} parked periods totalling #{periods.hours} hours"
    }
  end

  defp parked_in(snapshot, from) do
    entries = Enum.filter(snapshot.parked, &(DateTime.compare(&1.from, from) != :lt))
    hours = entries |> Enum.map(& &1.hours) |> Enum.reject(&is_nil/1) |> Value.sum()
    lost = entries |> Enum.map(& &1.energy_lost_kwh) |> Enum.reject(&is_nil/1) |> Value.sum()

    %{
      count: length(entries),
      hours: Value.round(hours, 1),
      energy_lost_kwh: Value.round(lost, 2),
      longest: Enum.sort_by(entries, & &1.hours, :desc)
    }
  end

  # -- tyre_pressures -------------------------------------------------------

  defp tyre_pressures(snapshot, args) do
    car = str_arg(args, "car", nil, nil)

    readings =
      snapshot.tyre_pressures
      |> Enum.filter(&matches_car?(&1, car))
      |> Enum.map(fn reading ->
        %{
          car: reading.car_name,
          recorded_at: iso(reading.recorded_at),
          front_left_bar: Value.round(reading.front_left_bar, 2),
          front_right_bar: Value.round(reading.front_right_bar, 2),
          rear_left_bar: Value.round(reading.rear_left_bar, 2),
          rear_right_bar: Value.round(reading.rear_right_bar, 2),
          last_30_days: reading.window
        }
      end)

    %{
      ok: true,
      readings: readings,
      notes: [
        if(readings == [],
          do: "No tyre pressure readings are stored for this car.",
          else: "Pressures are in bar. The window is min/max over the last 30 days."
        )
      ],
      _summary: "read the latest tyre pressures for #{length(readings)} car(s)"
    }
  end

  defp matches_car?(_reading, nil), do: true

  defp matches_car?(reading, car) when is_binary(car) do
    needle = String.downcase(car)
    String.contains?(String.downcase(reading.car_name || ""), needle)
  end

  # -- battery_health -------------------------------------------------------

  defp battery_health(snapshot, _args) do
    consumption =
      snapshot.drives
      |> Enum.reject(&is_nil(&1.consumption_kwh_100km))
      |> case do
        [] ->
          nil

        drives ->
          distance = Value.sum(Enum.map(drives, & &1.distance_km))
          energy = Value.sum(Enum.map(drives, & &1.energy_kwh))
          if distance > 0, do: Value.round(energy / distance * 100, 1)
      end

    cars =
      Enum.map(snapshot.battery, fn estimate ->
        range =
          if is_number(estimate.capacity_kwh) and is_number(consumption) and consumption > 0 do
            Value.round(estimate.capacity_kwh / consumption * 100, 0)
          end

        Map.merge(estimate, %{
          capacity_kwh: Value.round(estimate.capacity_kwh, 1),
          estimated_range_km: range
        })
      end)

    %{
      ok: true,
      cars: cars,
      observed_consumption_kwh_100km: consumption,
      notes: [
        "The capacity estimate is the energy added divided by the state-of-charge rise, summed over charge sessions longer than ten minutes ending at or below 95%.",
        "The estimated range divides that capacity by the observed consumption and is an estimate, not the car's own range figure."
      ],
      _summary: "read #{length(cars)} capacity estimate(s) from charge sessions"
    }
  end

  # -- data_coverage --------------------------------------------------------

  defp data_coverage(snapshot, _args) do
    coverage = snapshot.coverage

    %{
      ok: true,
      coverage: Map.drop(coverage, [:imports]),
      last_imports:
        Enum.map(coverage.imports, fn import ->
          %{
            source: import.source_key,
            status: import.status,
            ran_at: iso(import.ran_at),
            data_before: iso(import.date_limit)
          }
        end),
      notes: [
        "The agent reads at most #{snapshot.coverage.window_days} days and #{snapshot.coverage.drives_returned} drives per question."
        | window_notes(snapshot)
      ],
      _summary: "read the data coverage"
    }
  end

  # -- propose_chart --------------------------------------------------------

  defp propose_chart(snapshot, args) do
    case Chart.build(snapshot, args) do
      {:ok, chart} ->
        %{
          ok: true,
          chart: chart.id,
          kind: chart.kind,
          title: chart.title,
          note: "The chart is drawn under the answer. Refer to it as 'the chart below'.",
          _chart: %{title: chart.title, svg: chart.svg},
          _summary: "prepared the chart \"#{chart.title}\""
        }

      {:error, message} ->
        %{ok: false, error: message}
    end
  end

  # -- shared computations --------------------------------------------------

  defp window(snapshot, days) do
    days = days |> max(1) |> min(@max_days)
    from = DateTime.add(snapshot.now, -days * @day, :second)

    drives = Enum.filter(snapshot.drives, &after?(&1.started_at, from))
    charges = Enum.filter(snapshot.charges, &after?(&1.started_at, from))

    {from, drives, charges}
  end

  defp after?(nil, _from), do: false
  defp after?(at, from), do: DateTime.compare(at, from) != :lt

  defp drive_totals(drives) do
    distance = Value.sum(drives |> Enum.map(& &1.distance_km) |> Enum.reject(&is_nil/1))
    energy = Value.sum(drives |> Enum.map(& &1.energy_kwh) |> Enum.reject(&is_nil/1))
    temps = drives |> Enum.map(& &1.outside_temp_c) |> Enum.reject(&is_nil/1)

    %{
      drives: length(drives),
      drives_with_energy: Enum.count(drives, &is_number(&1.energy_kwh)),
      distance_km: Value.round(distance, 1),
      energy_kwh: Value.round(energy, 2),
      consumption_kwh_100km: if(distance > 0, do: Value.round(energy / distance * 100, 1)),
      avg_outside_temp_c: if(temps != [], do: Value.round(Value.sum(temps) / length(temps), 1))
    }
  end

  defp charge_totals(charges) do
    added = Value.sum(charges |> Enum.map(& &1.energy_added_kwh) |> Enum.reject(&is_nil/1))
    used = Value.sum(charges |> Enum.map(& &1.energy_used_kwh) |> Enum.reject(&is_nil/1))
    costs = charges |> Enum.map(& &1.cost) |> Enum.reject(&is_nil/1)

    %{
      sessions: length(charges),
      energy_added_kwh: Value.round(added, 2),
      energy_used_kwh: Value.round(used, 2),
      cost: if(costs != [], do: Value.round(Value.sum(costs), 2)),
      cost_sessions: length(costs),
      ac: Enum.count(charges, &(charge_kind(&1) == "AC")),
      dc: Enum.count(charges, &(charge_kind(&1) == "DC")),
      unknown_kind: Enum.count(charges, &(charge_kind(&1) == "unknown"))
    }
  end

  defp period(snapshot, from, days) do
    %{
      days: days,
      from: iso(from),
      to: iso(snapshot.now),
      tz: snapshot.tz,
      range: snapshot.range
    }
  end

  defp window_notes(snapshot) do
    coverage = snapshot.coverage

    cond do
      coverage.drives_truncated ->
        [
          "Only the most recent #{coverage.drives_returned} of #{coverage.drives_total} drives were loaded; the window is those drives, not the whole history."
        ]

      coverage.drives_total == 0 ->
        ["No drives have been imported yet."]

      true ->
        []
    end
  end

  defp iso(nil), do: nil
  defp iso(%DateTime{} = at), do: DateTime.to_iso8601(at)

  defp fmt(nil), do: "0"
  defp fmt(value) when is_number(value), do: :erlang.float_to_binary(value * 1.0, decimals: 0)

  # -- argument helpers -----------------------------------------------------

  defp int_arg(args, key, default) do
    case Map.get(args, key) do
      nil ->
        default

      value when is_integer(value) ->
        clamp(value, 1, @max_days)

      value when is_float(value) ->
        clamp(trunc(value), 1, @max_days)

      value when is_binary(value) ->
        case Integer.parse(value) do
          {number, _} -> clamp(number, 1, @max_days)
          :error -> default
        end

      _ ->
        default
    end
  end

  defp clamp(value, low, high), do: value |> max(low) |> min(high)

  defp str_arg(args, key, allowed, default) do
    value = Map.get(args, key)

    cond do
      is_binary(value) and (allowed == nil or value in allowed) -> value
      true -> default
    end
  end
end
