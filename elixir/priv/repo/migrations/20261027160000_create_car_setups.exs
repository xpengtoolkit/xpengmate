defmodule TeslaMate.Repo.Migrations.CreateCarSetups do
  use Ecto.Migration

  # One row per **aspect** of the car that was in a particular state over a
  # date range: which tyres were fitted, whether a trailer was being towed,
  # whether a roof rack/box was on, whether the car was loaded.
  #
  # A single row describing "the whole car" would force the aspects to change
  # together, which is not how they are used: tyres change twice a year, a
  # roof box goes on for a holiday, a trailer is towed for a day. So the rows
  # are per aspect and may overlap, and the analytics attribute a drive to the
  # most recently started row of each aspect that covers it.
  def change do
    create table(:car_setups) do
      # cars.id is smallint since 20200410112005; references/2 would default
      # this to bigint and create a mismatched foreign key.
      add :car_id, references(:cars, type: :smallint, on_delete: :delete_all), null: false
      # "tyres", "towing", "roof_rack", "loaded" or "other"; see
      # TeslaMate.Setups.Setup.aspects/0 for the authoritative list.
      add :aspect, :string, null: false
      # What the aspect was set to, in the owner's words: "Michelin Pilot Sport
      # 4 SUV 255/45R20", "Box trailer", "Roof box 400 L", "5 up + luggage".
      add :label, :string, null: false
      add :notes, :text
      # Inclusive date range. A NULL ended_at means "still in this setup";
      # COALESCE(ended_at, 'infinity') is how every query closes it.
      add :started_at, :date, null: false
      add :ended_at, :date

      timestamps(type: :utc_datetime_usec)
    end

    # Reads are "the setups for this car, newest first, usually of one aspect".
    create index(:car_setups, [:car_id, :aspect, :started_at])

    # The same aspect cannot start twice on the same day with the same label;
    # re-saving an edit therefore fails loudly instead of silently duplicating.
    create unique_index(:car_setups, [:car_id, :aspect, :label, :started_at],
             name: :car_setups_identity
           )

    create constraint(:car_setups, :car_setups_ended_after_started,
             check: "ended_at IS NULL OR ended_at >= started_at"
           )
  end
end
