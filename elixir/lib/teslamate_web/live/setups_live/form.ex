defmodule TeslaMateWeb.SetupsLive.Form do
  @moduledoc """
  New and edit form for one vehicle setup: the car, the aspect, a label, an
  inclusive date range and free-form notes.

  The label is not free-form in practice - the owner types a tyre model or a
  trailer - so `Setups.known_labels/0` populates a `datalist` to suggest what was
  used before for the selected aspect.
  """

  use TeslaMateWeb, :live_view

  alias TeslaMate.Setups
  alias TeslaMate.Setups.Setup
  alias TeslaMateWeb.SetupsLive

  on_mount {TeslaMateWeb.InitAssigns, :locale}

  @impl true
  def mount(%{"id" => id}, %{"settings" => _settings}, socket) do
    setup = Setups.get_setup!(id)

    {:ok, base_assigns(socket, setup, :edit)}
  end

  def mount(_params, %{"settings" => _settings}, socket) do
    setup = %Setup{started_at: Date.utc_today()}

    {:ok, base_assigns(socket, setup, :new)}
  end

  @impl true
  def handle_event("validate", %{"setup" => params}, socket) do
    changeset =
      socket.assigns.setup
      |> Setups.change_setup(params)
      |> Map.put(:action, :update)

    {:noreply, assign_form(socket, changeset)}
  end

  def handle_event("save", %{"setup" => params}, socket) do
    case save(socket.assigns.action, socket.assigns.setup, params) do
      {:ok, %Setup{} = setup} ->
        socket =
          socket
          |> put_flash(:success, flash_msg(socket.assigns.action, setup))
          |> push_navigate(to: Routes.live_path(socket, SetupsLive.Index))

        {:noreply, socket}

      {:error, %Ecto.Changeset{} = changeset} ->
        socket =
          socket
          |> assign(show_errors: true)
          |> assign_form(changeset)

        {:noreply, socket}
    end
  end

  # Private

  defp base_assigns(socket, %Setup{} = setup, action) when action in [:new, :edit] do
    known_labels = Setups.known_labels()

    socket
    |> assign(
      setup: setup,
      action: action,
      cars: Setups.list_cars(),
      known_labels: known_labels,
      show_errors: false,
      page_title: page_title(action)
    )
    |> assign_form(Setups.change_setup(setup))
  end

  defp assign_form(socket, %Ecto.Changeset{} = changeset) do
    assign(socket,
      changeset: changeset,
      label_suggestions: label_suggestions(socket.assigns.known_labels, changeset)
    )
  end

  # Suggest the labels already used for the selected aspect; before an aspect is
  # chosen (or when this aspect has none yet) every known label is useful.
  defp label_suggestions(known_labels, changeset) do
    aspect = Ecto.Changeset.get_field(changeset, :aspect)

    case aspect && Map.get(known_labels, aspect) do
      labels when is_list(labels) and labels != [] -> labels
      _ -> known_labels |> Map.values() |> List.flatten() |> Enum.uniq() |> Enum.sort()
    end
  end

  defp save(:new, %Setup{}, params), do: Setups.create_setup(params)
  defp save(:edit, %Setup{} = setup, params), do: Setups.update_setup(setup, params)

  defp page_title(:new), do: gettext("New setup")
  defp page_title(:edit), do: gettext("Edit setup")

  defp flash_msg(:new, %Setup{label: label}),
    do: gettext("Setup \"%{label}\" created", label: label)

  defp flash_msg(:edit, %Setup{label: label}),
    do: gettext("Setup \"%{label}\" updated", label: label)

  defp car_options(cars) do
    Enum.map(cars, fn car ->
      {car_label(car), car.id}
    end)
  end

  defp car_label(%TeslaMate.Log.Car{name: name, vin: vin}) when name in [nil, ""],
    do: gettext("VIN %{vin}", vin: vin)

  defp car_label(%TeslaMate.Log.Car{name: name}), do: name

  defp aspect_options do
    Enum.map(Setups.aspects(), fn aspect -> {aspect_label(aspect), aspect} end)
  end

  defp aspect_label(:tyres), do: gettext("Tyres")
  defp aspect_label(:towing), do: gettext("Towing")
  defp aspect_label(:roof_rack), do: gettext("Roof rack")
  defp aspect_label(:loaded), do: gettext("Loaded")
  defp aspect_label(:other), do: gettext("Other")
end
