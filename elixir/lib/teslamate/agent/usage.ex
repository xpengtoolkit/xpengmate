defmodule TeslaMate.Agent.Usage do
  @moduledoc """
  The assistant's daily question counter.

  A question is counted **before** the provider runs: a request that fails
  half-way has still been paid for, and a counter that only counted successes
  would let a client loop on errors for free. The increment and the cap are one
  statement, so two concurrent questions cannot both take the last slot.
  """

  import Ecto.Query, warn: false

  alias TeslaMate.Repo

  @doc "How many questions have been asked today (UTC day)."
  def used_today(day \\ Date.utc_today()) do
    Repo.one(from(u in "agent_usage", where: u.day == ^day, select: u.count)) || 0
  end

  @doc """
  Counts one question, or refuses when the day's allowance is spent.

  Returns `{:ok, used}` (the new count) or `{:error, :daily_limit}`.
  """
  def consume(limit, day \\ Date.utc_today()) when is_integer(limit) and limit > 0 do
    now = NaiveDateTime.utc_now() |> NaiveDateTime.truncate(:microsecond)

    sql = """
    INSERT INTO agent_usage (day, count, inserted_at, updated_at)
    VALUES ($1::date, 1, $2, $2)
    ON CONFLICT (day) DO UPDATE
       SET count = agent_usage.count + 1, updated_at = EXCLUDED.updated_at
     WHERE agent_usage.count < $3
    RETURNING count
    """

    case Repo.query(sql, [day, now, limit]) do
      {:ok, %{rows: [[count]]}} -> {:ok, count}
      {:ok, %{rows: []}} -> {:error, :daily_limit}
      {:error, reason} -> {:error, reason}
    end
  end
end
