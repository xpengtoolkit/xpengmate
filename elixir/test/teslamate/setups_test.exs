defmodule TeslaMate.SetupsTest do
  # Not async: the tests pin the deployment time zone (`TZ`) that drive dates
  # are attributed in, which is process-wide state.
  use TeslaMate.DataCase, async: false

  alias TeslaMate.{Log, Setups}
  alias TeslaMate.Log.Drive
  alias TeslaMate.Setups.Setup

  setup do
    previous = System.get_env("TZ")
    System.put_env("TZ", "UTC")

    on_exit(fn ->
      case previous do
        nil -> System.delete_env("TZ")
        tz -> System.put_env("TZ", tz)
      end
    end)

    :ok
  end

  defp car_fixture(attrs \\ %{}) do
    {:ok, car} =
      attrs
      |> Enum.into(%{
        efficiency: 0.153,
        eid: 42,
        model: "S",
        vid: 42,
        name: "foo",
        trim_badging: "P100D",
        vin: "12345F"
      })
      |> Log.create_car()

    car
  end

  defp drive_fixture(car, attrs \\ %{}) do
    %Drive{
      car_id: car.id,
      start_date: ~U[2026-01-15 09:00:00.000000Z],
      end_date: ~U[2026-01-15 10:00:00.000000Z],
      distance: 50.0,
      duration_min: 60,
      outside_temp_avg: Decimal.new("20.0"),
      start_rated_range_km: Decimal.new("300.00"),
      end_rated_range_km: Decimal.new("250.00"),
      start_ideal_range_km: Decimal.new("300.00"),
      end_ideal_range_km: Decimal.new("250.00")
    }
    |> struct(attrs)
    |> Repo.insert!()
  end

  defp setup_fixture(car, attrs \\ %{}) do
    {:ok, setup} =
      attrs
      |> Enum.into(%{
        car_id: car.id,
        aspect: :tyres,
        label: "Michelin Pilot Sport 4 255/45R20",
        started_at: ~D[2026-01-01]
      })
      |> Setups.create_setup()

    setup
  end

  describe "create_setup/1" do
    test "stores a setup and lists it with its car" do
      car = car_fixture()

      assert {:ok, %Setup{} = setup} =
               Setups.create_setup(%{
                 car_id: car.id,
                 aspect: :roof_rack,
                 label: "Roof box 400 L",
                 notes: "Holiday",
                 started_at: ~D[2026-01-10],
                 ended_at: ~D[2026-01-20]
               })

      assert setup.aspect == :roof_rack
      assert setup.label == "Roof box 400 L"
      assert [listed] = Setups.list_setups()
      assert listed.id == setup.id
      assert listed.car.id == car.id
    end

    test "trims the label and treats a blank note as no note" do
      car = car_fixture()

      assert {:ok, setup} =
               Setups.create_setup(%{
                 car_id: car.id,
                 aspect: :towing,
                 label: "  Box trailer  ",
                 notes: "   ",
                 started_at: ~D[2026-01-10]
               })

      assert setup.label == "Box trailer"
      assert setup.notes == nil
    end

    test "requires a label" do
      car = car_fixture()

      assert {:error, changeset} =
               Setups.create_setup(%{
                 car_id: car.id,
                 aspect: :tyres,
                 label: "   ",
                 started_at: ~D[2026-01-10]
               })

      assert "can't be blank" in errors_on(changeset).label
    end

    test "rejects an end date before the start date" do
      car = car_fixture()

      assert {:error, changeset} =
               Setups.create_setup(%{
                 car_id: car.id,
                 aspect: :loaded,
                 label: "5 up + luggage",
                 started_at: ~D[2026-01-10],
                 ended_at: ~D[2026-01-09]
               })

      assert "must be on or after the start date" in errors_on(changeset).ended_at
    end

    test "refuses the same aspect, label and start date twice" do
      car = car_fixture()
      setup_fixture(car)

      assert {:error, changeset} =
               Setups.create_setup(%{
                 car_id: car.id,
                 aspect: :tyres,
                 label: "Michelin Pilot Sport 4 255/45R20",
                 started_at: ~D[2026-01-01]
               })

      assert "has already been taken" in errors_on(changeset).car_id
    end
  end

  describe "update_setup/2 and delete_setup/1" do
    test "updates an existing setup" do
      setup = setup_fixture(car_fixture())

      assert {:ok, updated} =
               Setups.update_setup(setup, %{label: "Winter tyres", ended_at: ~D[2026-02-01]})

      assert updated.label == "Winter tyres"
      assert updated.ended_at == ~D[2026-02-01]
    end

    test "deletes a setup" do
      setup = setup_fixture(car_fixture())

      assert {:ok, _} = Setups.delete_setup(setup)
      assert Setups.list_setups() == []
      assert_raise Ecto.NoResultsError, fn -> Setups.get_setup!(setup.id) end
    end
  end

  describe "known_labels/0" do
    test "lists the labels already used per aspect, sorted" do
      car = car_fixture()

      setup_fixture(car, %{label: "Summer tyres"})
      setup_fixture(car, %{label: "All-season tyres", started_at: ~D[2025-02-01]})
      setup_fixture(car, %{aspect: :towing, label: "Box trailer", started_at: ~D[2025-03-01]})

      assert %{
               tyres: ["All-season tyres", "Summer tyres"],
               towing: ["Box trailer"]
             } = Setups.known_labels()
    end
  end

  describe "drive_counts/0" do
    test "counts the drives each setup covers, including an open-ended setup" do
      car = car_fixture()
      january = setup_fixture(car, %{started_at: ~D[2026-01-01], ended_at: ~D[2026-01-31]})
      current = setup_fixture(car, %{label: "Winter tyres", started_at: ~D[2026-02-01]})

      drive_fixture(car, %{start_date: ~U[2026-01-15 09:00:00.000000Z]})
      drive_fixture(car, %{start_date: ~U[2026-01-20 09:00:00.000000Z]})
      drive_fixture(car, %{start_date: ~U[2026-03-05 09:00:00.000000Z]})

      assert Setups.drive_counts() == %{january.id => 2, current.id => 1}
    end
  end

  describe "comparison/0" do
    test "attributes each drive to the tyres that were on the car, with a baseline" do
      car = car_fixture()

      setup_fixture(car, %{
        label: "All-season",
        started_at: ~D[2026-01-01],
        ended_at: ~D[2026-01-31]
      })

      setup_fixture(car, %{label: "Winter", started_at: ~D[2026-02-01]})

      drive_fixture(car, %{start_date: ~U[2026-01-15 09:00:00.000000Z]})
      drive_fixture(car, %{start_date: ~U[2026-02-10 09:00:00.000000Z]})
      drive_fixture(car, %{start_date: ~U[2025-12-10 09:00:00.000000Z]})

      rows = Setups.comparison()

      all_season = Enum.find(rows, &(&1.aspect == :tyres and &1.label == "All-season"))
      winter = Enum.find(rows, &(&1.aspect == :tyres and &1.label == "Winter"))
      baseline = Enum.find(rows, &(&1.aspect == :tyres and is_nil(&1.label)))

      assert all_season.drives == 1
      assert_in_delta all_season.distance_km, 50.0, 0.001
      # 50 km of rated range at 0.153 kWh/km.
      assert_in_delta all_season.energy_kwh, 7.65, 0.001
      assert_in_delta all_season.consumption_wh_km, 153.0, 0.01
      assert_in_delta all_season.avg_outside_temp_c, 20.0, 0.01
      assert_in_delta all_season.avg_speed_kmh, 50.0, 0.01

      assert winter.drives == 1
      assert winter.car_name == "foo"
      assert all_season.car_id == car.id

      assert baseline.drives == 1
    end

    test "attributes one drive to several aspects at once" do
      car = car_fixture()

      setup_fixture(car, %{aspect: :tyres, label: "Summer", started_at: ~D[2026-01-01]})

      setup_fixture(car, %{
        aspect: :roof_rack,
        label: "Roof box",
        started_at: ~D[2026-01-10],
        ended_at: ~D[2026-01-20]
      })

      drive_fixture(car, %{start_date: ~U[2026-01-15 09:00:00.000000Z]})

      rows = Setups.comparison()

      assert Enum.find(rows, &(&1.aspect == :tyres and &1.label == "Summer")).drives == 1
      assert Enum.find(rows, &(&1.aspect == :roof_rack and &1.label == "Roof box")).drives == 1
    end

    test "keeps two cars with the same name apart" do
      first = car_fixture(%{name: "The car"})
      second = car_fixture(%{name: "The car", eid: 43, vid: 43, vin: "67890F"})

      setup_fixture(first, %{label: "Summer", started_at: ~D[2026-01-01]})
      setup_fixture(second, %{aspect: :towing, label: "Trailer", started_at: ~D[2026-01-01]})

      drive_fixture(first, %{start_date: ~U[2026-01-15 09:00:00.000000Z]})
      drive_fixture(second, %{start_date: ~U[2026-01-15 09:00:00.000000Z]})

      rows = Setups.comparison() |> Enum.filter(&(&1.car_name == "The car"))

      assert Enum.find(rows, &(&1.aspect == :tyres and &1.car_id == first.id)).drives == 1
      # The second car has no tyres setup at all, so it has no tyres row to compare.
      assert Enum.find(rows, &(&1.aspect == :tyres and &1.car_id == second.id)) == nil

      assert Enum.find(rows, &(&1.aspect == :towing and &1.car_id == second.id)).label ==
               "Trailer"
    end

    test "prefers the newest setup when two of one aspect overlap" do
      car = car_fixture()

      setup_fixture(car, %{label: "Old tyres", started_at: ~D[2026-01-01]})
      setup_fixture(car, %{label: "New tyres", started_at: ~D[2026-01-10]})

      drive_fixture(car, %{start_date: ~U[2026-01-15 09:00:00.000000Z]})

      rows = Setups.comparison() |> Enum.filter(&(&1.aspect == :tyres))

      assert [%{label: "New tyres", drives: 1}] = rows
    end

    test "returns nothing when no setups exist" do
      car = car_fixture()
      drive_fixture(car)

      assert Setups.comparison() == []
    end

    test "uses the deployment's local day, not the UTC day" do
      car = car_fixture()

      # 22:00Z on 14 January is 09:00 on 15 January in Sydney.
      drive_fixture(car, %{start_date: ~U[2026-01-14 22:00:00.000000Z]})

      setup_fixture(car, %{label: "Fitted on the 15th", started_at: ~D[2026-01-15]})

      assert [%{label: nil, drives: 1}] =
               Setups.comparison() |> Enum.filter(&(&1.aspect == :tyres))

      System.put_env("TZ", "Australia/Sydney")

      assert [%{label: "Fitted on the 15th", drives: 1}] =
               Setups.comparison() |> Enum.filter(&(&1.aspect == :tyres))
    end
  end
end
