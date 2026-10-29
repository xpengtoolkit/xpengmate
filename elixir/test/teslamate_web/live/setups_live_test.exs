defmodule TeslaMateWeb.SetupsLiveTest do
  use TeslaMateWeb.ConnCase

  alias TeslaMate.{Log, Repo, Setups, Settings}
  alias TeslaMate.Log.Drive

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

  describe "Index" do
    test "renders the empty state when there are no cars", %{conn: conn} do
      assert {:ok, _view, html} = live(conn, "/setups")

      assert html =~ "No cars yet"
    end

    test "renders the empty state when cars exist but no setups do", %{conn: conn} do
      car_fixture()

      assert {:ok, _view, html} = live(conn, "/setups")

      assert html =~ "No setups recorded yet"
    end

    test "renders a setup with its date range and drive count", %{conn: conn} do
      car = car_fixture()
      setup = setup_fixture(car)
      drive_fixture(car)

      assert {:ok, _view, html} = live(conn, "/setups")

      row =
        html
        |> Floki.parse_document!()
        |> Floki.find("#setup_#{setup.id}")
        |> List.first()

      cells =
        row
        |> Floki.find("td")
        |> Enum.map(&(&1 |> Floki.text() |> String.trim()))

      assert Enum.at(cells, 0) == "Michelin Pilot Sport 4 255/45R20"
      assert Enum.at(cells, 1) == "2026-01-01"
      assert Enum.at(cells, 2) == "Current"
      assert Enum.at(cells, 3) == "1"
    end

    test "warns about overlapping ranges for the same car and aspect", %{conn: conn} do
      car = car_fixture()

      january =
        setup_fixture(car, %{started_at: ~D[2026-01-01], ended_at: ~D[2026-01-31]})

      overlapping =
        setup_fixture(car, %{label: "Winter tyres", started_at: ~D[2026-01-15]})

      assert {:ok, _view, html} = live(conn, "/setups")
      html = Floki.parse_document!(html)

      for setup <- [january, overlapping] do
        assert html |> Floki.find("#setup_#{setup.id}") |> Floki.text() =~
                 "Overlapping range"
      end

      assert Floki.text(html) =~ "newest start date"
    end

    test "does not warn when the ranges do not overlap", %{conn: conn} do
      car = car_fixture()

      first = setup_fixture(car, %{started_at: ~D[2026-01-01], ended_at: ~D[2026-01-31]})
      second = setup_fixture(car, %{label: "Winter tyres", started_at: ~D[2026-02-01]})

      assert {:ok, _view, html} = live(conn, "/setups")
      html = Floki.parse_document!(html)

      for setup <- [first, second] do
        refute html |> Floki.find("#setup_#{setup.id}") |> Floki.text() =~
                 "Overlapping range"
      end

      refute Floki.text(html) =~ "newest start date"
    end

    test "renders the comparison table with a baseline row", %{conn: conn} do
      car = car_fixture()

      setup_fixture(car, %{
        label: "Winter tyres",
        started_at: ~D[2026-01-01],
        ended_at: ~D[2026-01-31]
      })

      # Covered by the tyres setup.
      drive_fixture(car)

      # Not covered by any tyres setup: the baseline.
      drive_fixture(car, %{
        start_date: ~U[2026-02-15 09:00:00.000000Z],
        end_date: ~U[2026-02-15 10:00:00.000000Z]
      })

      assert {:ok, _view, html} = live(conn, "/setups")
      html = Floki.parse_document!(html)

      table = html |> Floki.find("table[data-aspect=\"tyres\"]") |> List.first()
      refute is_nil(table)

      text = Floki.text(table)
      assert text =~ "Winter tyres"
      assert text =~ "Not recorded"
      assert text =~ "153.0 Wh/km"
    end

    test "sorts each aspect by consumption ascending", %{conn: conn} do
      car = car_fixture()

      setup_fixture(car, %{
        label: "Thirsty",
        started_at: ~D[2026-01-01],
        ended_at: ~D[2026-01-31]
      })

      setup_fixture(car, %{
        label: "Frugal",
        started_at: ~D[2026-02-01],
        ended_at: ~D[2026-02-28]
      })

      # 50 km of rated range: 153.0 Wh/km.
      drive_fixture(car)

      # 25 km of rated range: 76.5 Wh/km.
      drive_fixture(car, %{
        start_date: ~U[2026-02-15 09:00:00.000000Z],
        end_date: ~U[2026-02-15 10:00:00.000000Z],
        end_rated_range_km: Decimal.new("275.00")
      })

      assert {:ok, _view, html} = live(conn, "/setups")
      html = Floki.parse_document!(html)

      assert ["Frugal", "Thirsty"] =
               html
               |> Floki.find("table[data-aspect=\"tyres\"] tbody tr")
               |> Enum.map(fn row ->
                 row |> Floki.find("td") |> List.first() |> Floki.text() |> String.trim()
               end)
    end

    test "renders the comparison in miles when the unit of length is mi", %{conn: conn} do
      car = car_fixture()
      setup_fixture(car)
      drive_fixture(car)

      {:ok, _settings} =
        Settings.get_global_settings!()
        |> Settings.update_global_settings(%{unit_of_length: :mi})

      assert {:ok, _view, html} = live(conn, "/setups")

      assert html =~ "Wh/mi"
      assert html =~ "31.1 mi"
    end

    test "deletes a setup and reloads the page", %{conn: conn} do
      car = car_fixture()
      setup = setup_fixture(car)

      assert {:ok, view, html} = live(conn, "/setups")
      assert html =~ "Michelin Pilot Sport 4 255/45R20"

      html = render_click(view, "delete", %{"id" => "#{setup.id}"})

      refute Floki.text(Floki.parse_document!(html)) =~ "Michelin Pilot Sport 4 255/45R20"
      assert html =~ "No setups recorded yet"
      assert Setups.list_setups() == []
    end
  end

  describe "New" do
    test "pre-fills the start date with today", %{conn: conn} do
      assert {:ok, _view, html} = live(conn, "/setups/new")

      assert [value] =
               html
               |> Floki.parse_document!()
               |> Floki.find("#setup_started_at")
               |> Floki.attribute("value")

      assert value == Date.to_string(Date.utc_today())
    end

    test "creates a setup", %{conn: conn} do
      car = car_fixture()

      assert {:ok, view, _html} = live(conn, "/setups/new")

      render_submit(view, :save, %{
        "setup" => %{
          "car_id" => "#{car.id}",
          "aspect" => "tyres",
          "label" => "Winter tyres",
          "started_at" => "2026-01-01",
          "ended_at" => "",
          "notes" => "  "
        }
      })

      assert_redirect(view, "/setups")

      assert [setup] = Setups.list_setups()
      assert setup.label == "Winter tyres"
      assert setup.car_id == car.id
      assert setup.started_at == ~D[2026-01-01]
      assert setup.ended_at == nil
      assert setup.notes == nil
    end

    test "shows changeset errors and does not create an invalid setup", %{conn: conn} do
      car = car_fixture()

      assert {:ok, view, _html} = live(conn, "/setups/new")

      html =
        render_submit(view, :save, %{
          "setup" => %{
            "car_id" => "#{car.id}",
            "aspect" => "tyres",
            "label" => "",
            "started_at" => "2026-01-10",
            "ended_at" => "2026-01-01"
          }
        })

      text = html |> Floki.parse_document!() |> Floki.text()

      assert text =~ "can't be blank"
      assert text =~ "must be on or after the start date"
      assert Setups.list_setups() == []
    end

    test "suggests the labels already used for the selected aspect", %{conn: conn} do
      car = car_fixture()
      setup_fixture(car, %{label: "Summer tyres"})

      setup_fixture(car, %{
        aspect: :towing,
        label: "Box trailer",
        started_at: ~D[2026-02-01]
      })

      assert {:ok, view, _html} = live(conn, "/setups/new")

      html =
        view
        |> render_change(:validate, %{"setup" => %{"aspect" => "tyres"}})
        |> Floki.parse_document!()

      options = Floki.find(html, "#known-labels option") |> Floki.attribute("value")

      assert "Summer tyres" in options
      refute "Box trailer" in options
    end
  end

  describe "Edit" do
    test "pre-fills the setup and updates it", %{conn: conn} do
      car = car_fixture()
      setup = setup_fixture(car)

      assert {:ok, view, html} = live(conn, "/setups/#{setup.id}/edit")

      assert [value] =
               html
               |> Floki.parse_document!()
               |> Floki.find("#setup_label")
               |> Floki.attribute("value")

      assert value == "Michelin Pilot Sport 4 255/45R20"

      render_submit(view, :save, %{
        "setup" => %{
          "car_id" => "#{car.id}",
          "aspect" => "tyres",
          "label" => "Michelin CrossClimate",
          "started_at" => "2026-01-01",
          "ended_at" => "2026-06-30",
          "notes" => "Changed before summer"
        }
      })

      assert_redirect(view, "/setups")

      updated = Setups.get_setup!(setup.id)
      assert updated.label == "Michelin CrossClimate"
      assert updated.ended_at == ~D[2026-06-30]
      assert updated.notes == "Changed before summer"
    end
  end
end
