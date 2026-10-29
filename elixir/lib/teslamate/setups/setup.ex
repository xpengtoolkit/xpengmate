defmodule TeslaMate.Setups.Setup do
  @moduledoc """
  One aspect of a car's setup over an inclusive date range.

  A row answers "what tyres were on this car between these dates" or "was a
  trailer being towed then"; the aspects are independent, so a tyre change and
  a roof box can overlap. `ended_at` of `nil` means the setup is still on the
  car, and every query closes the range with
  `COALESCE(ended_at, 'infinity'::date)`.

  Rows are deliberately not linked to drives. A drive is attributed to a setup
  when it is read, from its date, so correcting a date range re-tags history
  instead of leaving stored tags behind.
  """

  use Ecto.Schema
  import Ecto.Changeset

  alias TeslaMate.Log.Car

  @aspects [:tyres, :towing, :roof_rack, :loaded, :other]

  @type t :: %__MODULE__{}

  schema "car_setups" do
    field :aspect, Ecto.Enum, values: @aspects
    field :label, :string
    field :notes, :string
    field :started_at, :date
    field :ended_at, :date

    belongs_to :car, Car

    timestamps(type: :utc_datetime_usec)
  end

  @doc "The aspects a setup can describe, in display order."
  def aspects, do: @aspects

  @doc false
  def changeset(setup, attrs) do
    setup
    |> cast(attrs, [:car_id, :aspect, :label, :notes, :started_at, :ended_at])
    |> update_change(:label, &String.trim/1)
    |> update_change(:notes, &trim_to_nil/1)
    |> validate_required([:car_id, :aspect, :label, :started_at])
    |> validate_length(:label, max: 120)
    |> validate_length(:notes, max: 2000)
    |> validate_end_after_start()
    |> unique_constraint([:car_id, :aspect, :label, :started_at], name: :car_setups_identity)
    |> foreign_key_constraint(:car_id)
    |> check_constraint(:ended_at, name: :car_setups_ended_after_started)
  end

  # The database has the same rule as a CHECK constraint; failing here as well
  # is what puts the message on the field the user typed in.
  defp validate_end_after_start(changeset) do
    with started_at when not is_nil(started_at) <- get_field(changeset, :started_at),
         ended_at when not is_nil(ended_at) <- get_field(changeset, :ended_at),
         true <- Date.before?(ended_at, started_at) do
      add_error(changeset, :ended_at, "must be on or after the start date")
    else
      _ -> changeset
    end
  end

  defp trim_to_nil(nil), do: nil

  defp trim_to_nil(notes) do
    case String.trim(notes) do
      "" -> nil
      trimmed -> trimmed
    end
  end
end
