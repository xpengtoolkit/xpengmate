defmodule TeslaMate.Agent.ChartTest do
  use ExUnit.Case, async: true

  alias TeslaMate.Agent.Chart
  alias TeslaMate.AgentSnapshotFixture

  test "renders a bar chart of monthly consumption" do
    assert {:ok, chart} =
             Chart.build(AgentSnapshotFixture.snapshot(), %{
               "chart" => "monthly_consumption",
               "kind" => "bar"
             })

    assert chart.title == "Consumption per month"
    assert chart.svg =~ "<svg"
    assert chart.svg =~ "<rect"
    assert chart.svg =~ "kWh/100 km"
    assert length(chart.points) == 2
  end

  test "renders a line chart with one series" do
    assert {:ok, chart} =
             Chart.build(AgentSnapshotFixture.snapshot(), %{
               "chart" => "monthly_distance",
               "kind" => "line"
             })

    assert chart.svg =~ "<polyline"
    assert chart.svg =~ "km"
  end

  test "renders charging energy and the setup comparison" do
    assert {:ok, charging} =
             Chart.build(AgentSnapshotFixture.snapshot(), %{
               "chart" => "charging_energy",
               "kind" => "bar"
             })

    assert charging.svg =~ "Energy added per month"

    assert {:ok, setups} =
             Chart.build(
               AgentSnapshotFixture.snapshot(),
               %{"chart" => "setup_comparison", "kind" => "bar", "aspect" => "tyres"}
             )

    assert setups.svg =~ "Winter tyres"
    assert setups.svg =~ "Summer tyres"
  end

  test "escapes a model-supplied title instead of trusting it" do
    assert {:ok, chart} =
             Chart.build(
               AgentSnapshotFixture.snapshot(),
               %{
                 "chart" => "monthly_distance",
                 "kind" => "bar",
                 "title" => "<script>alert(1)</script>"
               }
             )

    refute chart.svg =~ "<script>"
    assert chart.svg =~ "&lt;script&gt;"
  end

  test "refuses an unknown chart, an unknown kind, and a line of categories" do
    snapshot = AgentSnapshotFixture.snapshot()

    assert {:error, message} = Chart.build(snapshot, %{"chart" => "pie"})
    assert message =~ "Unknown chart"

    assert {:error, message} =
             Chart.build(snapshot, %{"chart" => "monthly_distance", "kind" => "scatter"})

    assert message =~ "bar or line"

    assert {:error, message} =
             Chart.build(snapshot, %{
               "chart" => "setup_comparison",
               "kind" => "line",
               "aspect" => "tyres"
             })

    assert message =~ "bar chart"
  end

  test "refuses to draw a chart when the snapshot holds no data" do
    snapshot = AgentSnapshotFixture.snapshot(%{drives: [], charges: []})

    assert {:error, message} =
             Chart.build(snapshot, %{"chart" => "monthly_consumption", "kind" => "bar"})

    assert message =~ "no driving data"
  end
end
