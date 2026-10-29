defmodule TeslaMate.AgentSnapshotFixture do
  @moduledoc """
  A hand-written `TeslaMate.Agent.Snapshot.load/1` result.

  The tools and the chart renderer are pure functions of this map, so these
  tests need neither a database nor a model: the numbers are chosen so every
  expected figure can be worked out by hand.
  """

  def snapshot(overrides \\ %{}) do
    now = ~U[2026-06-15 12:00:00.000000Z]

    Map.merge(
      %{
        now: now,
        today: ~D[2026-06-15],
        tz: "Australia/Sydney",
        range: "rated",
        cars: [
          %{
            id: 1,
            name: "XPENG G6",
            model: "G6",
            vin: "LXD0000000000001",
            efficiency: 0.153
          }
        ],
        drives: [
          drive(%{
            id: 1,
            started_at: ~U[2026-06-10 08:00:00.000000Z],
            distance_km: 50.0,
            energy_kwh: 7.65,
            consumption_kwh_100km: 15.3,
            outside_temp_c: 18.0,
            aspects: %{tyres: "Winter tyres"},
            start_address: "Home",
            end_address: "Work"
          }),
          drive(%{
            id: 2,
            started_at: ~U[2026-06-11 08:00:00.000000Z],
            distance_km: 100.0,
            energy_kwh: 18.0,
            consumption_kwh_100km: 18.0,
            outside_temp_c: 14.0,
            aspects: %{tyres: "Winter tyres", roof_rack: "Roof box"},
            start_address: "Work",
            end_address: "Beach"
          }),
          drive(%{
            id: 3,
            started_at: ~U[2026-05-02 08:00:00.000000Z],
            distance_km: 200.0,
            energy_kwh: 30.0,
            consumption_kwh_100km: 15.0,
            outside_temp_c: 20.0,
            aspects: %{tyres: "Summer tyres"},
            start_address: "Home",
            end_address: "Airport"
          })
        ],
        charges: [
          charge(%{
            id: 10,
            started_at: ~U[2026-06-09 20:00:00.000000Z],
            energy_added_kwh: 40.0,
            energy_used_kwh: 44.0,
            start_soc: 20,
            end_soc: 70,
            duration_min: 45,
            peak_power_kw: 120.0,
            cost: 16.0
          }),
          charge(%{
            id: 11,
            started_at: ~U[2026-06-12 22:00:00.000000Z],
            energy_added_kwh: 10.0,
            energy_used_kwh: 11.0,
            start_soc: 60,
            end_soc: 72,
            duration_min: 120,
            peak_power_kw: 11.0,
            cost: 3.0
          })
        ],
        setups: [
          %{
            id: 1,
            car_id: 1,
            car_name: "XPENG G6",
            aspect: :tyres,
            label: "Winter tyres",
            started_at: ~D[2026-05-15],
            ended_at: nil
          },
          %{
            id: 2,
            car_id: 1,
            car_name: "XPENG G6",
            aspect: :tyres,
            label: "Summer tyres",
            started_at: ~D[2026-01-01],
            ended_at: ~D[2026-05-14]
          },
          %{
            id: 3,
            car_id: 1,
            car_name: "XPENG G6",
            aspect: :roof_rack,
            label: "Roof box",
            started_at: ~D[2026-06-11],
            ended_at: nil
          }
        ],
        parked: [
          %{
            car_id: 1,
            car_name: "XPENG G6",
            from: ~U[2026-06-10 09:00:00.000000Z],
            to: ~U[2026-06-11 08:00:00.000000Z],
            hours: 23.0,
            energy_lost_kwh: 0.9
          },
          %{
            car_id: 1,
            car_name: "XPENG G6",
            from: ~U[2026-06-11 10:00:00.000000Z],
            to: ~U[2026-06-12 08:00:00.000000Z],
            hours: 22.0,
            energy_lost_kwh: nil
          }
        ],
        tyre_pressures: [
          %{
            car_id: 1,
            car_name: "XPENG G6",
            recorded_at: ~U[2026-06-15 06:00:00.000000Z],
            front_left_bar: 2.6,
            front_right_bar: 2.6,
            rear_left_bar: 2.8,
            rear_right_bar: 2.7,
            window: %{
              min_fl: 2.4,
              max_fl: 2.7,
              min_fr: 2.4,
              max_fr: 2.7,
              min_rl: 2.5,
              max_rl: 2.9,
              min_rr: 2.5,
              max_rr: 2.9
            }
          }
        ],
        battery: [
          %{car_id: 1, car_name: "XPENG G6", capacity_kwh: 80.0, samples: 10}
        ],
        coverage: %{
          drives_total: 3,
          charges_total: 2,
          first_drive_at: ~U[2026-05-02 08:00:00.000000Z],
          last_drive_at: ~U[2026-06-11 08:00:00.000000Z],
          first_charge_at: ~U[2026-06-09 20:00:00.000000Z],
          last_charge_at: ~U[2026-06-12 22:00:00.000000Z],
          drives_returned: 3,
          drives_truncated: false,
          charges_returned: 2,
          charges_truncated: false,
          window_days: 730,
          imports: []
        }
      },
      overrides
    )
  end

  def drive(overrides) do
    Map.merge(
      %{
        id: 0,
        car_id: 1,
        car_name: "XPENG G6",
        started_at: ~U[2026-06-10 08:00:00.000000Z],
        ended_at: ~U[2026-06-10 09:00:00.000000Z],
        distance_km: 50.0,
        duration_min: 60,
        energy_kwh: 7.65,
        consumption_kwh_100km: 15.3,
        outside_temp_c: 18.0,
        speed_max_kmh: 90,
        ascent_m: nil,
        descent_m: nil,
        start_address: nil,
        end_address: nil,
        start_range_km: 400.0,
        end_range_km: 350.0,
        aspects: %{}
      },
      overrides
    )
  end

  def charge(overrides) do
    Map.merge(
      %{
        id: 0,
        car_id: 1,
        car_name: "XPENG G6",
        started_at: ~U[2026-06-09 20:00:00.000000Z],
        duration_min: 45,
        energy_added_kwh: 40.0,
        energy_used_kwh: 44.0,
        start_soc: 20,
        end_soc: 70,
        cost: 16.0,
        location: "Home",
        peak_power_kw: 120.0
      },
      overrides
    )
  end
end
