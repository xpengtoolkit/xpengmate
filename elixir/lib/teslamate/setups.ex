defmodule TeslaMate.Setups do
  @moduledoc """
  Which setup a car was in over time: the tyres that were fitted, whether a
  trailer was towed, whether a roof box was on, whether the car was loaded.

  A setup is one **aspect** over an inclusive date range (see
  `TeslaMate.Setups.Setup`), so aspects are independent and may overlap.

  Nothing is written onto drives. `comparison/0` attributes each drive to the
  newest setup of each aspect that covers the drive's date, at read time - so
  fixing a mistyped date re-tags the history instead of leaving stale tags on
  rows nothing would remember to clean up.

  The calendar day a drive belongs to is the deployment's own day
  (`TZ`, falling back to UTC), not the UTC day: a 07:00 drive in Sydney is the
  previous UTC day, and it is the local day the owner fitted the tyres on.
  """

  import Ecto.Query, warn: false

  alias TeslaMate.{Log, Repo, Settings}
  alias TeslaMate.Setups.Setup

  ## Setups

  @doc """
  All setups, with their car preloaded.

  Ordering is by car, then aspect, then newest first, so a page can render the
  rows in the order they are read without sorting again.
  """
  def list_setups do
    Setup
    |> order_by([s], asc: s.car_id, asc: s.aspect, desc: s.started_at)
    |> preload(:car)
    |> Repo.all()
  end

  def get_setup!(id) do
    Repo.get!(Setup, id)
    |> Repo.preload(:car)
  end

  def create_setup(attrs \\ %{}) do
    %Setup{}
    |> Setup.changeset(attrs)
    |> Repo.insert()
  end

  def update_setup(%Setup{} = setup, attrs) do
    setup
    |> Setup.changeset(attrs)
    |> Repo.update()
  end

  def delete_setup(%Setup{} = setup), do: Repo.delete(setup)

  @doc "A changeset for forms; an empty map yields an unsaved setup."
  def change_setup(%Setup{} = setup, attrs \\ %{}), do: Setup.changeset(setup, attrs)

  @doc "The aspects a setup can describe, in display order."
  def aspects, do: Setup.aspects()

  @doc "The cars a setup can be attached to."
  def list_cars, do: Log.list_cars()

  @doc """
  The labels already used for each aspect, so a form can suggest them and the
  owner does not have to type "Michelin Pilot Sport 4" identically twice.

  Returns a map of aspect to a sorted list of labels.
  """
  def known_labels do
    Setup
    |> group_by([s], [s.aspect, s.label])
    |> select([s], {s.aspect, s.label})
    |> Repo.all()
    |> Enum.reduce(%{}, fn {aspect, label}, acc ->
      Map.update(acc, aspect, [label], &[label | &1])
    end)
    |> Map.new(fn {aspect, labels} -> {aspect, Enum.sort(labels)} end)
  end

  @doc """
  How many drives each setup covers, keyed by setup id.

  One query for the whole page rather than one per row.
  """
  def drive_counts do
    sql = """
    SELECT s.id, count(d.id)
    FROM car_setups s
    LEFT JOIN drives d
      ON d.car_id = s.car_id
     AND #{local_date("d.start_date")} BETWEEN s.started_at AND COALESCE(s.ended_at, 'infinity'::date)
    GROUP BY s.id
    """

    %{rows: rows} = Repo.query!(sql, [timezone()])

    Map.new(rows, fn [id, count] -> {id, count} end)
  end

  ## Comparison

  @doc """
  Distance, energy and consumption per setup aspect and label, across all cars.

  Rows are keyed by `car_id` (and carry `car_name`) so two cars that happen to
  share a name are never aggregated together.

  A drive is attributed to the newest setup of each aspect that covers it, so
  one drive can appear under "tyres" and again under "roof_rack". Drives that
  no setup of an aspect covers appear with a `nil` label: that is the baseline
  a roof box or a trailer is compared against.

  Energy is TeslaMate's own estimate - the drop in the car's `preferred_range`
  range times the car's rated `efficiency` - so drives without a usable range
  drop, a distance or a car efficiency are left out. That is the same basis the
  Efficiency dashboard uses, so the two agree.

  `consumption_wh_km` is `nil` when the distance aggregate is zero or the
  energy could not be estimated. Formatting into the owner's units is the
  caller's job.
  """
  def comparison do
    range = preferred_range_key()

    sql = """
    WITH aspects AS (
      SELECT DISTINCT car_id, aspect
      FROM car_setups
    ),
    base AS (
      SELECT d.id,
             d.car_id,
             COALESCE(NULLIF(c.name, ''), 'Car ' || c.id::text) AS car_name,
             c.efficiency,
             d.start_date,
             d.distance,
             d.duration_min,
             d.outside_temp_avg,
             d.start_#{range}_range_km - d.end_#{range}_range_km AS range_km
      FROM drives d
      JOIN cars c ON c.id = d.car_id
      WHERE d.start_date IS NOT NULL
        AND d.distance IS NOT NULL
        AND c.efficiency IS NOT NULL
        AND d.start_#{range}_range_km IS NOT NULL
        AND d.end_#{range}_range_km IS NOT NULL
        AND d.start_#{range}_range_km - d.end_#{range}_range_km >= 0.1
    ),
    tagged AS (
      SELECT b.*,
             a.aspect,
             (SELECT s.label
                FROM car_setups s
               WHERE s.car_id = b.car_id
                 AND s.aspect = a.aspect
                 AND #{local_date("b.start_date")} BETWEEN s.started_at AND COALESCE(s.ended_at, 'infinity'::date)
               ORDER BY s.started_at DESC
               LIMIT 1) AS label
      FROM base b
      JOIN aspects a ON a.car_id = b.car_id
    )
    SELECT car_id,
           car_name,
           aspect,
           label,
           count(*) AS drives,
           sum(distance) AS distance_km,
           sum(range_km * efficiency) AS energy_kwh,
           avg(outside_temp_avg) AS avg_outside_temp_c,
           sum(distance) / NULLIF(sum(duration_min) / 60.0, 0) AS avg_speed_kmh
    FROM tagged
    GROUP BY car_id, car_name, aspect, label
    ORDER BY car_id, aspect, label NULLS FIRST
    """

    %{columns: columns, rows: rows} = Repo.query!(sql, [timezone()])
    columns = Enum.map(columns, &String.to_atom/1)

    rows
    |> Enum.map(fn row -> columns |> Enum.zip(row) |> Map.new() end)
    |> Enum.map(&normalise_comparison_row/1)
  end

  defp normalise_comparison_row(row) do
    distance_km = number(row[:distance_km])
    energy_kwh = number(row[:energy_kwh])

    row
    |> Map.put(:aspect, lookup_aspect(row[:aspect]))
    |> Map.put(:distance_km, distance_km)
    |> Map.put(:energy_kwh, energy_kwh)
    |> Map.put(:avg_outside_temp_c, number(row[:avg_outside_temp_c]))
    |> Map.put(:avg_speed_kmh, number(row[:avg_speed_kmh]))
    |> Map.put(
      :consumption_wh_km,
      if(distance_km && distance_km > 0 && energy_kwh,
        do: energy_kwh / distance_km * 1000.0
      )
    )
  end

  # The database returns the Ecto.Enum value as a string; map it back to the
  # atom the schema uses, without creating atoms from row data.
  defp lookup_aspect(aspect) when is_binary(aspect) do
    Enum.find(Setup.aspects(), aspect, &(Atom.to_string(&1) == aspect))
  end

  defp lookup_aspect(aspect), do: aspect

  defp number(nil), do: nil
  defp number(%Decimal{} = value), do: Decimal.to_float(value)
  defp number(value) when is_integer(value), do: value * 1.0
  defp number(value) when is_float(value), do: value

  defp preferred_range_key do
    case Settings.get_global_settings!().preferred_range do
      :ideal -> "ideal"
      _ -> "rated"
    end
  end

  @doc """
  The time zone drive dates are attributed in: the deployment's `TZ`, falling
  back to UTC when it is unset or not a zone the time zone database knows.
  """
  def timezone do
    case System.get_env("TZ") do
      tz when is_binary(tz) and tz != "" ->
        case DateTime.from_naive(~N[2020-01-01 00:00:00], tz) do
          {:ok, _} -> tz
          _ -> "UTC"
        end

      _ ->
        "UTC"
    end
  end

  # `start_date` is a timestamp without a time zone holding UTC. Attach UTC,
  # convert to the deployment's zone, then take the calendar day.
  defp local_date(expr) do
    "(timezone($1, timezone('UTC', #{expr})))::date"
  end
end
