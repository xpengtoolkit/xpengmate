defmodule TeslaMate.Repo.Migrations.CreateAgentSettings do
  use Ecto.Migration

  # One row of assistant configuration: the provider key (encrypted with the
  # same Vault/ENCRYPTION_KEY as the Tesla tokens, so it is not readable from a
  # database dump or a backup), the model, and the switches that bound the cost.
  #
  # A table rather than environment variables because the owner should be able
  # to paste a key into the settings page without a rebuild or a restart.
  def change do
    create table(:agent_settings) do
      add :singleton, :boolean, null: false, default: true
      add :api_key, :binary
      add :model, :string, null: false, default: "deepseek-flash"
      add :base_url, :string, null: false, default: "https://api.deepseek.com"
      # Off until an owner turns it on: a key being present is not consent to
      # spend money on every question someone asks the page.
      add :enabled, :boolean, null: false, default: false
      add :daily_limit, :integer, null: false, default: 60

      timestamps(type: :utc_datetime_usec)
    end

    create unique_index(:agent_settings, [:singleton], name: :agent_settings_singleton)

    create constraint(:agent_settings, :agent_settings_daily_limit_range,
             check: "daily_limit > 0 AND daily_limit <= 1000"
           )

    # One row per day: the same counter Redis holds in the EVConduit backend,
    # but this stack has no Redis and Postgres is already here.
    create table(:agent_usage) do
      add :day, :date, null: false
      add :count, :integer, null: false, default: 0

      timestamps(type: :utc_datetime_usec)
    end

    create unique_index(:agent_usage, [:day], name: :agent_usage_day)
  end
end
