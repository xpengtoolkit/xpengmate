import Config

config :logger, level: :warning

# We don't run a server during test. If one is required,
# you can enable the server option below.
config :teslamate, TeslaMateWeb.Endpoint, server: false
config :teslamate, TeslaMate.Repo, pool: Ecto.Adapters.SQL.Sandbox

config :phoenix, :plug_init_mode, :runtime

# The Ask page's mascot settles back to idle after this long; 6 s in production.
config :teslamate, :pengu_awake_ms, 50
