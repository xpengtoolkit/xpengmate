defmodule TeslaMate.Agent.Settings do
  @moduledoc """
  The assistant's configuration, stored as a single row.

  The provider key is encrypted at rest (`TeslaMate.Vault.Encrypted.Binary`),
  so the same `ENCRYPTION_KEY` that protects the Tesla tokens protects it, and
  it is redacted from `inspect/1` and logs. It is never sent back to a browser:
  the settings page shows only whether one is set.
  """

  use Ecto.Schema
  import Ecto.Changeset

  alias TeslaMate.Vault.Encrypted

  @type t :: %__MODULE__{}

  schema "agent_settings" do
    field :singleton, :boolean, default: true
    field :api_key, Encrypted.Binary, redact: true
    field :model, :string, default: "deepseek-flash"
    field :base_url, :string, default: "https://api.deepseek.com"
    field :enabled, :boolean, default: false
    field :daily_limit, :integer, default: 60

    timestamps(type: :utc_datetime_usec)
  end

  @doc false
  def changeset(settings, attrs) do
    settings
    |> cast(attrs, [:api_key, :model, :base_url, :enabled, :daily_limit])
    |> validate_required([:model, :base_url, :daily_limit])
    |> validate_length(:model, max: 120)
    |> validate_length(:base_url, max: 200)
    |> validate_format(:base_url, ~r{^https?://})
    |> validate_number(:daily_limit, greater_than: 0, less_than_or_equal_to: 1000)
  end
end
