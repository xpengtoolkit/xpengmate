defmodule TeslaMate.Agent.Value do
  @moduledoc """
  Small conversions shared by the snapshot and the tools.

  Postgres returns `numeric` as `Decimal` and `float8` as `float`; the tools do
  arithmetic on both, so everything numeric is converted to a float once, at the
  edge. A missing measurement stays `nil` and never becomes zero.
  """

  def num(nil), do: nil
  def num(%Decimal{} = value), do: Decimal.to_float(value)
  def num(value) when is_integer(value), do: value * 1.0
  def num(value) when is_float(value), do: value

  def round(nil, _places), do: nil
  def round(value, places) when is_number(value), do: Float.round(value * 1.0, places)

  def sum(values) do
    values
    |> Enum.reject(&is_nil/1)
    |> Enum.reduce(0.0, fn value, acc -> acc + value * 1.0 end)
  end

  @doc "Convert a UTC timestamp without zone to a DateTime, for JSON and maths."
  def utc(nil), do: nil
  def utc(%DateTime{} = value), do: value

  def utc(%NaiveDateTime{} = value) do
    DateTime.from_naive!(value, "Etc/UTC")
  end
end
