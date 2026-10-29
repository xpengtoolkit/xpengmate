defmodule TeslaMate.Agent.ToolsTest do
  use ExUnit.Case, async: true

  alias TeslaMate.Agent.{Tools}
  alias TeslaMate.AgentSnapshotFixture

  test "every tool is advertised with a JSON function schema" do
    names = Tools.names()

    assert "overview" in names
    assert "consumption_by_setup" in names
    assert "propose_chart" in names
    assert length(names) == 10

    for schema <- Tools.schemas() do
      assert schema["type"] == "function"
      assert is_binary(schema["function"]["name"])
      assert schema["function"]["parameters"]["type"] == "object"
    end
  end

  test "overview totals the window and separates wall energy from pack energy" do
    result = Tools.execute(AgentSnapshotFixture.snapshot(), "overview", %{"days" => 30})

    assert result.ok
    assert result.driving.drives == 2
    assert_in_delta result.driving.distance_km, 150.0, 0.01
    assert_in_delta result.driving.energy_kwh, 25.65, 0.01
    assert_in_delta result.driving.consumption_kwh_100km, 17.1, 0.01

    assert result.charging.sessions == 2
    assert_in_delta result.charging.energy_added_kwh, 50.0, 0.01
    assert_in_delta result.charging.energy_used_kwh, 55.0, 0.01
    assert result.charging.ac == 1
    assert result.charging.dc == 1

    assert result.parked.count == 2
    assert_in_delta result.parked.energy_lost_kwh, 0.9, 0.01
    assert result._summary == "read 2 drives and 2 charges over 30 days"
  end

  test "driving_series buckets in the owner's timezone" do
    result =
      Tools.execute(AgentSnapshotFixture.snapshot(), "driving_series", %{
        "days" => 365,
        "bucket" => "month"
      })

    assert result.ok
    assert result.bucket == "month"
    assert [may, june] = result.points
    assert may.bucket == "2026-05-01"
    assert_in_delta may.distance_km, 200.0, 0.01
    assert_in_delta june.distance_km, 150.0, 0.01
    assert_in_delta june.consumption_kwh_100km, 17.1, 0.01
  end

  test "consumption_by_setup attributes drives to the newest covering setup" do
    result =
      Tools.execute(AgentSnapshotFixture.snapshot(), "consumption_by_setup", %{"days" => 365})

    assert result.ok
    tyres = Enum.filter(result.rows, &(&1.aspect == "tyres"))
    assert Enum.map(tyres, & &1.label) == ["Summer tyres", "Winter tyres"]

    winter = Enum.find(tyres, &(&1.label == "Winter tyres"))
    assert winter.drives == 2
    assert_in_delta winter.distance_km, 150.0, 0.01
    assert_in_delta winter.consumption_kwh_100km, 17.1, 0.01

    summer = Enum.find(tyres, &(&1.label == "Summer tyres"))
    assert summer.drives == 1
    assert_in_delta summer.consumption_kwh_100km, 15.0, 0.01

    roof = Enum.find(result.rows, &(&1.aspect == "roof_rack" and &1.label == "Roof box"))
    assert roof.drives == 1
    baseline = Enum.find(result.rows, &(&1.aspect == "roof_rack" and &1.label == "Not recorded"))
    assert baseline.drives == 2
  end

  test "consumption_by_setup answers a single aspect" do
    result =
      Tools.execute(
        AgentSnapshotFixture.snapshot(),
        "consumption_by_setup",
        %{"days" => 365, "aspect" => "tyres"}
      )

    assert Enum.all?(result.rows, &(&1.aspect == "tyres"))
  end

  test "charging filters by AC and DC, derived from peak power" do
    snapshot = AgentSnapshotFixture.snapshot()

    dc = Tools.execute(snapshot, "charging", %{"days" => 30, "kind" => "DC"})
    assert dc.kind == "DC"
    assert dc.totals.sessions == 1
    assert [%{kind: "DC", energy_added_kwh: 40.0}] = dc.sessions

    ac = Tools.execute(snapshot, "charging", %{"days" => 30, "kind" => "AC"})
    assert ac.totals.sessions == 1
    assert [%{kind: "AC", peak_power_kw: 11.0}] = ac.sessions
  end

  test "trips sorts by distance and carries the setups the drive ran on" do
    result =
      Tools.execute(AgentSnapshotFixture.snapshot(), "trips", %{
        "days" => 365,
        "sort" => "distance",
        "limit" => 3
      })

    assert [longest, middle, shortest] = result.drives
    assert_in_delta longest.distance_km, 200.0, 0.01
    assert_in_delta middle.distance_km, 100.0, 0.01
    assert_in_delta shortest.distance_km, 50.0, 0.01
    assert middle.setups.tyres == "Winter tyres"
    assert middle.setups.roof_rack == "Roof box"
  end

  test "parked reports the periods between drives and says what it cannot know" do
    result = Tools.execute(AgentSnapshotFixture.snapshot(), "parked", %{"days" => 30})

    assert result.ok
    assert result.totals.periods == 2
    assert_in_delta result.totals.hours, 45.0, 0.01
    assert_in_delta result.totals.energy_lost_kwh, 0.9, 0.01
    assert Enum.any?(result.notes, &String.contains?(&1, "between consecutive drives"))
  end

  test "tyre_pressures reports the latest reading and the 30-day window" do
    result = Tools.execute(AgentSnapshotFixture.snapshot(), "tyre_pressures", %{})

    assert [reading] = result.readings
    assert reading.car == "XPENG G6"
    assert_in_delta reading.front_left_bar, 2.6, 0.001
    assert_in_delta reading.rear_left_bar, 2.8, 0.001
    assert_in_delta reading.last_30_days.min_fl, 2.4, 0.001
  end

  test "battery_health derives a capacity and a range from the observed consumption" do
    result = Tools.execute(AgentSnapshotFixture.snapshot(), "battery_health", %{})

    assert [car] = result.cars
    assert_in_delta car.capacity_kwh, 80.0, 0.01
    assert car.samples == 10
    assert_in_delta result.observed_consumption_kwh_100km, 15.9, 0.1
    assert_in_delta car.estimated_range_km, 503.0, 1.0
  end

  test "data_coverage reports totals and truncation honestly" do
    result = Tools.execute(AgentSnapshotFixture.snapshot(), "data_coverage", %{})

    assert result.coverage.drives_total == 3
    assert result.coverage.drives_truncated == false
  end

  test "data_coverage tells the model when the drive window was truncated" do
    base = AgentSnapshotFixture.snapshot()
    coverage = %{base.coverage | drives_total: 5000, drives_truncated: true}
    snapshot = %{base | coverage: coverage}

    result = Tools.execute(snapshot, "data_coverage", %{})
    assert result.coverage.drives_truncated
    assert Enum.any?(result.notes, &String.contains?(&1, "most recent 3 of 5000"))
  end

  test "propose_chart validates the catalogue and returns a chart to draw" do
    result =
      Tools.execute(
        AgentSnapshotFixture.snapshot(),
        "propose_chart",
        %{"chart" => "setup_comparison", "kind" => "bar", "aspect" => "tyres"}
      )

    assert result.ok
    assert result.chart == "setup_comparison"
    assert result._chart.title == "Consumption by tyres"
    assert result._chart.svg =~ "<svg"
    assert result._chart.svg =~ "Winter tyres"
  end

  test "propose_chart refuses a line chart of categories and an unknown chart" do
    line =
      Tools.execute(
        AgentSnapshotFixture.snapshot(),
        "propose_chart",
        %{"chart" => "setup_comparison", "kind" => "line"}
      )

    assert line.ok == false
    assert line.error =~ "bar chart"

    unknown = Tools.execute(AgentSnapshotFixture.snapshot(), "propose_chart", %{"chart" => "pie"})
    assert unknown.ok == false
    assert unknown.error =~ "Unknown chart"
  end

  test "an unknown tool is an error result, not a crash" do
    assert %{ok: false, error: "Unknown tool: nope"} =
             Tools.execute(AgentSnapshotFixture.snapshot(), "nope", %{})
  end

  test "clamps a silly window instead of failing" do
    result = Tools.execute(AgentSnapshotFixture.snapshot(), "overview", %{"days" => 99_999})
    assert result.period.days == 730
  end
end
