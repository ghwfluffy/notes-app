(() => {
  "use strict";

  const configuredBase = window.NOTES_CONFIG?.basePath || "";
  const basePath = configuredBase === "/" ? "" : configuredBase.replace(/\/$/, "");
  const apiBase = `${basePath}/api/v1`;
  const REQUEST_TIMEOUT_MS = 15000;
  const state = {
    me: null,
    lists: [],
    selectedListId: null,
    reorderListIds: [],
  };

  const byId = (id) => document.getElementById(id);
  const appLayout = byId("app-layout");
  const loadingState = byId("loading-state");
  const fatalError = byId("fatal-error");
  const listNavigation = byId("list-navigation");
  const listPanel = byId("list-panel");
  const newListButton = byId("new-list-button");
  const reorderListsButton = byId("reorder-lists-button");
  const listDialog = byId("list-dialog");
  const reorderDialog = byId("reorder-dialog");
  const reorderList = byId("reorder-list");
  const itemDialog = byId("item-dialog");
  let toastTimer = null;
  let latestLoadAttempt = 0;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showToast(message, isError = false) {
    const toast = byId("toast");
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 3200);
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(`${apiBase}${path}`, {
        ...options,
        headers: {
          "content-type": "application/json",
          ...(options.headers || {}),
        },
        signal: controller.signal,
      });
      if (response.status === 401) {
        window.location.assign(`${apiBase}/auth/oauth/login?next=${encodeURIComponent(`${basePath}/`)}`);
        throw new Error("Authentication required.");
      }
      let payload = null;
      try {
        payload = await response.json();
      } catch (error) {
        if (controller.signal.aborted) throw error;
      }
      if (!response.ok) {
        const detail = typeof payload?.detail === "string" ? payload.detail : "The request could not be completed.";
        throw new Error(detail);
      }
      return payload;
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error("My Notes took too long to respond. Try again.");
      }
      if (error instanceof TypeError) {
        throw new Error("My Notes could not connect. Check your connection and try again.");
      }
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  function selectedList() {
    return state.lists.find((list) => list.id === state.selectedListId) || state.lists[0] || null;
  }

  function configureBanner() {
    const banner = byId("federated-banner");
    if (!banner || !state.me) return;
    banner.setAttribute("app-url", `${basePath}/` || "/");
    banner.setAttribute("account-settings-url", state.me.accountSettingsUrl || "");
    banner.sites = state.me.federatedApps || [];
    banner.user = state.me.user || null;
    banner.addEventListener("federated-banner-action", (event) => {
      if (event.detail?.action === "sign-out") {
        window.location.assign(`${apiBase}/auth/logout`);
      }
    });
  }

  function renderNavigation() {
    const navigationItems = [];
    for (const noteList of state.lists) {
      const button = element("button", "list-button");
      button.type = "button";
      button.dataset.listId = noteList.id;
      button.setAttribute("aria-current", String(noteList.id === state.selectedListId));
      button.style.setProperty("--list-color", noteList.color);
      button.append(
        element("span", "list-dot"),
        element("span", "list-button-name", noteList.name),
        element("span", "list-count", String(noteList.active_item_count)),
      );
      button.addEventListener("click", () => {
        state.selectedListId = noteList.id;
        render();
        listPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
      navigationItems.push(button);
    }
    listNavigation.replaceChildren(...navigationItems, newListButton, reorderListsButton);
  }

  function iconButton(label, symbol, onClick, danger = false) {
    const button = element("button", `icon-button${danger ? " button-danger" : ""}`, symbol);
    button.type = "button";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.addEventListener("click", onClick);
    return button;
  }

  function focusItemMoveControl(itemId, direction) {
    const row = [...document.querySelectorAll(".item-card")]
      .find((candidate) => candidate.dataset.itemId === itemId);
    const requested = row?.querySelector(`[data-item-direction="${direction}"]`);
    const fallback = row?.querySelector(".item-move-button:not(:disabled)")
      || row?.querySelector(".item-content");
    (requested && !requested.disabled ? requested : fallback)?.focus();
  }

  async function moveItem(item, noteList, orderedItems, direction, controls) {
    const currentIndex = orderedItems.findIndex((candidate) => candidate.id === item.id);
    const targetIndex = currentIndex + (direction === "up" ? -1 : 1);
    const target = orderedItems[targetIndex];
    if (!target) return;
    for (const button of controls.querySelectorAll("button")) button.disabled = true;
    try {
      await request(`/items/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ position: target.position }),
      });
      await reloadLists(noteList.id);
      focusItemMoveControl(item.id, direction);
      showToast(`Moved ${direction}.`);
    } catch (error) {
      for (const button of controls.querySelectorAll("button")) button.disabled = false;
      showToast(error.message, true);
    }
  }

  function itemCard(item, noteList, orderedItems = null) {
    const row = element("li", `item-card${item.completed ? " completed" : ""}`);
    row.dataset.itemId = item.id;

    const checkLabel = element("label", "item-check");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = item.completed;
    checkbox.setAttribute("aria-label", `${item.completed ? "Mark active" : "Mark complete"}: ${item.title}`);
    checkbox.addEventListener("change", async () => {
      checkbox.disabled = true;
      try {
        await request(`/items/${encodeURIComponent(item.id)}`, {
          method: "PATCH",
          body: JSON.stringify({ completed: checkbox.checked }),
        });
        await reloadLists(noteList.id);
        showToast(checkbox.checked ? "Marked complete." : "Moved back to active.");
      } catch (error) {
        checkbox.checked = !checkbox.checked;
        checkbox.disabled = false;
        showToast(error.message, true);
      }
    });
    checkLabel.append(checkbox);

    const content = element("button", "item-content");
    content.type = "button";
    content.append(element("span", "item-title", item.title));
    if (item.details) content.append(element("span", "item-details", item.details));
    content.addEventListener("click", () => openItemDialog(item));

    const menu = element("div", "item-menu");
    if (orderedItems && orderedItems.length > 1) {
      const itemIndex = orderedItems.findIndex((candidate) => candidate.id === item.id);
      const controls = element("div", "item-move-controls");
      controls.setAttribute("role", "group");
      controls.setAttribute("aria-label", `Move ${item.title}`);
      const up = iconButton(
        `Move ${item.title} up`,
        "↑",
        () => moveItem(item, noteList, orderedItems, "up", controls),
      );
      up.classList.add("item-move-button");
      up.dataset.itemDirection = "up";
      up.disabled = itemIndex === 0;
      const down = iconButton(
        `Move ${item.title} down`,
        "↓",
        () => moveItem(item, noteList, orderedItems, "down", controls),
      );
      down.classList.add("item-move-button");
      down.dataset.itemDirection = "down";
      down.disabled = itemIndex === orderedItems.length - 1;
      controls.append(up, down);
      menu.append(controls);
    }
    const edit = iconButton("Edit item", "⋯", () => openItemDialog(item));
    edit.classList.add("item-edit-button");
    menu.append(edit);
    row.append(checkLabel, content, menu);
    return row;
  }

  function emptyState(title, message) {
    const box = element("div", "empty-state");
    const content = element("div");
    content.append(element("strong", "", title), element("span", "", message));
    box.append(content);
    return box;
  }

  function renderSelectedList() {
    const noteList = selectedList();
    if (!noteList) {
      listPanel.append(emptyState("Start a list", "Create a list for anything you want to remember."));
      return;
    }

    const heading = element("div", "panel-heading");
    const copy = element("div");
    copy.append(element("p", "eyebrow", `${noteList.active_item_count} active`));
    copy.append(element("h2", "", noteList.name));
    if (noteList.description) copy.append(element("p", "", noteList.description));
    const actions = element("div", "panel-actions");
    actions.append(
      iconButton("Edit list", "✎", () => openListDialog(noteList)),
      iconButton("Delete list", "⌫", () => deleteList(noteList), true),
    );
    heading.append(copy, actions);

    const quickAdd = element("form", "quick-add");
    const input = document.createElement("input");
    input.required = true;
    input.maxLength = 500;
    input.placeholder = `Add to ${noteList.name}…`;
    input.setAttribute("aria-label", `Add item to ${noteList.name}`);
    const submit = element("button", "button button-primary", "Add item");
    submit.type = "submit";
    quickAdd.append(input, submit);
    quickAdd.addEventListener("submit", async (event) => {
      event.preventDefault();
      const title = input.value.trim();
      if (!title) return;
      input.disabled = true;
      submit.disabled = true;
      try {
        await request(`/lists/${encodeURIComponent(noteList.id)}/items`, {
          method: "POST",
          body: JSON.stringify({ title }),
        });
        await reloadLists(noteList.id);
        showToast("Added to your list.");
        window.setTimeout(() => document.querySelector(".quick-add input")?.focus(), 0);
      } catch (error) {
        input.disabled = false;
        submit.disabled = false;
        showToast(error.message, true);
      }
    });

    listPanel.append(heading, quickAdd);
    if (!noteList.items.length) {
      listPanel.append(emptyState("This list is ready", "Add the first thing you want to remember."));
      return;
    }
    const active = noteList.items.filter((item) => !item.completed);
    const completed = noteList.items.filter((item) => item.completed);
    const items = element("ul", "items");
    for (const group of [active, completed]) {
      for (const item of group) items.append(itemCard(item, noteList, group));
    }
    listPanel.append(items);
  }

  function render() {
    if (!state.selectedListId && state.lists.length) state.selectedListId = state.lists[0].id;
    if (state.selectedListId && !state.lists.some((list) => list.id === state.selectedListId)) {
      state.selectedListId = state.lists[0]?.id || null;
    }
    renderNavigation();
    reorderListsButton.disabled = state.lists.length < 2;
    listPanel.replaceChildren();
    renderSelectedList();
  }

  async function reloadLists(preferredListId = state.selectedListId) {
    const payload = await request("/lists");
    state.lists = payload.lists || [];
    state.selectedListId = preferredListId;
    render();
  }

  function openListDialog(noteList = null) {
    byId("list-dialog-title").textContent = noteList ? "Edit list" : "New list";
    byId("list-id").value = noteList?.id || "";
    byId("list-name").value = noteList?.name || "";
    byId("list-description").value = noteList?.description || "";
    byId("list-color").value = noteList?.color || "#6750a4";
    listDialog.showModal();
    window.setTimeout(() => byId("list-name").focus(), 0);
  }

  function focusReorderControl(listId, direction = null) {
    const row = [...reorderList.children].find((candidate) => candidate.dataset.listId === listId);
    const requestedControl = direction
      ? row?.querySelector(`[data-direction="${direction}"]`)
      : row?.querySelector(".reorder-grip");
    const control = requestedControl && !requestedControl.disabled
      ? requestedControl
      : row?.querySelector(".reorder-grip");
    control?.focus();
  }

  function announceReorder(listId) {
    const noteList = state.lists.find((candidate) => candidate.id === listId);
    const position = state.reorderListIds.indexOf(listId);
    if (!noteList || position < 0) return;
    byId("reorder-status").textContent = `${noteList.name} is now ${position + 1} of ${state.reorderListIds.length}.`;
  }

  function moveReorderList(listId, offset, direction) {
    const currentIndex = state.reorderListIds.indexOf(listId);
    const nextIndex = currentIndex + offset;
    if (currentIndex < 0 || nextIndex < 0 || nextIndex >= state.reorderListIds.length) return;
    state.reorderListIds.splice(currentIndex, 1);
    state.reorderListIds.splice(nextIndex, 0, listId);
    renderReorderList();
    focusReorderControl(listId, direction);
    announceReorder(listId);
  }

  function startPointerReorder(event, row, grip) {
    if (!event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    const listId = row.dataset.listId;
    const startingIndex = state.reorderListIds.indexOf(listId);
    grip.setPointerCapture(event.pointerId);
    row.classList.add("dragging");
    reorderList.classList.add("is-dragging");

    const move = (moveEvent) => {
      const target = document.elementFromPoint(moveEvent.clientX, moveEvent.clientY)?.closest(".reorder-row");
      if (!target || target === row || target.parentElement !== reorderList) return;
      const targetBounds = target.getBoundingClientRect();
      if (moveEvent.clientY < targetBounds.top + targetBounds.height / 2) {
        reorderList.insertBefore(row, target);
      } else {
        reorderList.insertBefore(row, target.nextSibling);
      }
      updateReorderPositions();
    };

    const finish = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", finish);
      grip.removeEventListener("pointercancel", finish);
      row.classList.remove("dragging");
      reorderList.classList.remove("is-dragging");
      state.reorderListIds = [...reorderList.children].map((candidate) => candidate.dataset.listId);
      const changed = state.reorderListIds.indexOf(listId) !== startingIndex;
      renderReorderList();
      focusReorderControl(listId);
      if (changed) announceReorder(listId);
    };

    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", finish);
    grip.addEventListener("pointercancel", finish);
  }

  function updateReorderPositions() {
    for (const [index, row] of [...reorderList.children].entries()) {
      const position = row.querySelector(".reorder-position");
      if (position) position.textContent = String(index + 1);
    }
  }

  function renderReorderList() {
    reorderList.replaceChildren();
    for (const [index, listId] of state.reorderListIds.entries()) {
      const noteList = state.lists.find((candidate) => candidate.id === listId);
      if (!noteList) continue;
      const row = element("li", "reorder-row");
      row.dataset.listId = noteList.id;
      row.style.setProperty("--list-color", noteList.color);

      const position = element("span", "reorder-position", String(index + 1));
      position.setAttribute("aria-hidden", "true");

      const grip = element("button", "reorder-grip");
      grip.type = "button";
      grip.append(element("span", "reorder-grip-icon", "⠿"), element("span", "", "Drag"));
      grip.firstElementChild?.setAttribute("aria-hidden", "true");
      grip.setAttribute("aria-label", `Drag ${noteList.name}. Position ${index + 1} of ${state.reorderListIds.length}. Use arrow keys to move.`);
      grip.setAttribute("aria-describedby", "reorder-help");
      grip.setAttribute("aria-keyshortcuts", "ArrowUp ArrowDown Home End");
      grip.title = `Drag ${noteList.name}`;
      grip.addEventListener("pointerdown", (event) => startPointerReorder(event, row, grip));
      grip.addEventListener("keydown", (event) => {
        if (event.key === "ArrowUp") {
          event.preventDefault();
          moveReorderList(noteList.id, -1, null);
        } else if (event.key === "ArrowDown") {
          event.preventDefault();
          moveReorderList(noteList.id, 1, null);
        } else if (event.key === "Home") {
          event.preventDefault();
          moveReorderList(noteList.id, -index, null);
        } else if (event.key === "End") {
          event.preventDefault();
          moveReorderList(noteList.id, state.reorderListIds.length - index - 1, null);
        }
      });

      const copy = element("div", "reorder-copy");
      copy.append(element("span", "list-dot"), element("span", "reorder-name", noteList.name));

      const controls = element("div", "reorder-controls");
      controls.setAttribute("role", "group");
      controls.setAttribute("aria-label", `Move ${noteList.name}`);
      const up = element("button", "reorder-step", "↑");
      up.type = "button";
      up.dataset.direction = "up";
      up.disabled = index === 0;
      up.setAttribute("aria-label", `Move ${noteList.name} up`);
      up.title = `Move ${noteList.name} up`;
      up.addEventListener("click", () => moveReorderList(noteList.id, -1, "up"));
      const down = element("button", "reorder-step", "↓");
      down.type = "button";
      down.dataset.direction = "down";
      down.disabled = index === state.reorderListIds.length - 1;
      down.setAttribute("aria-label", `Move ${noteList.name} down`);
      down.title = `Move ${noteList.name} down`;
      down.addEventListener("click", () => moveReorderList(noteList.id, 1, "down"));
      controls.append(up, down);
      row.append(position, copy, grip, controls);
      reorderList.append(row);
    }
  }

  function openReorderDialog() {
    state.reorderListIds = state.lists.map((noteList) => noteList.id);
    byId("reorder-status").textContent = "";
    renderReorderList();
    reorderDialog.showModal();
    window.setTimeout(() => reorderList.querySelector(".reorder-grip")?.focus(), 0);
  }

  function openItemDialog(item) {
    byId("item-id").value = item.id;
    byId("item-title").value = item.title;
    byId("item-details").value = item.details || "";
    byId("item-completed").checked = item.completed;
    itemDialog.showModal();
    window.setTimeout(() => byId("item-title").focus(), 0);
  }

  async function deleteList(noteList) {
    if (!window.confirm(`Delete “${noteList.name}” and its ${noteList.item_count} item${noteList.item_count === 1 ? "" : "s"}?`)) return;
    try {
      await request(`/lists/${encodeURIComponent(noteList.id)}`, { method: "DELETE" });
      state.selectedListId = null;
      await reloadLists();
      showToast("List deleted.");
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function deleteItem(item) {
    if (!window.confirm(`Delete “${item.title}”?`)) return;
    try {
      await request(`/items/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      itemDialog.close();
      await reloadLists();
      showToast("Item deleted.");
    } catch (error) {
      showToast(error.message, true);
    }
  }

  newListButton.addEventListener("click", () => openListDialog());
  reorderListsButton.addEventListener("click", openReorderDialog);
  byId("retry-button").addEventListener("click", () => load());

  byId("list-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = byId("list-id").value;
    const saveButton = byId("save-list-button");
    saveButton.disabled = true;
    try {
      const payload = {
        name: byId("list-name").value.trim(),
        description: byId("list-description").value.trim() || null,
        color: byId("list-color").value,
      };
      const saved = await request(id ? `/lists/${encodeURIComponent(id)}` : "/lists", {
        method: id ? "PATCH" : "POST",
        body: JSON.stringify(payload),
      });
      listDialog.close();
      await reloadLists(saved.id);
      showToast(id ? "List updated." : "List created.");
    } catch (error) {
      showToast(error.message, true);
    } finally {
      saveButton.disabled = false;
    }
  });

  byId("reorder-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const saveButton = byId("save-order-button");
    saveButton.disabled = true;
    try {
      const payload = await request("/lists/order", {
        method: "PUT",
        body: JSON.stringify({ list_ids: state.reorderListIds }),
      });
      state.lists = payload.lists || [];
      reorderDialog.close();
      render();
      showToast("List order saved.");
    } catch (error) {
      showToast(error.message, true);
    } finally {
      saveButton.disabled = false;
    }
  });

  byId("item-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = byId("item-id").value;
    const saveButton = byId("save-item-button");
    saveButton.disabled = true;
    try {
      const payload = {
        title: byId("item-title").value.trim(),
        details: byId("item-details").value.trim() || null,
        completed: byId("item-completed").checked,
      };
      await request(`/items/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      itemDialog.close();
      await reloadLists();
      showToast("Item updated.");
    } catch (error) {
      showToast(error.message, true);
    } finally {
      saveButton.disabled = false;
    }
  });

  const itemActions = byId("item-form").querySelector(".modal-actions");
  const deleteItemButton = element("button", "button button-danger", "Delete");
  deleteItemButton.type = "button";
  deleteItemButton.addEventListener("click", () => {
    const id = byId("item-id").value;
    const item = state.lists.flatMap((list) => list.items).find((candidate) => candidate.id === id);
    if (item) deleteItem(item);
  });
  itemActions.prepend(deleteItemButton);

  for (const dialog of [listDialog, reorderDialog, itemDialog]) {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  }
  for (const button of document.querySelectorAll("[data-dialog-close]")) {
    button.addEventListener("click", () => button.closest("dialog")?.close());
  }

  async function load() {
    const loadAttempt = ++latestLoadAttempt;
    loadingState.hidden = false;
    fatalError.hidden = true;
    appLayout.hidden = true;
    try {
      const [me, listsPayload] = await Promise.all([request("/auth/me"), request("/lists")]);
      if (loadAttempt !== latestLoadAttempt) return;
      state.me = me;
      state.lists = listsPayload.lists || [];
      state.selectedListId = state.lists[0]?.id || null;
      configureBanner();
      render();
      loadingState.hidden = true;
      fatalError.hidden = true;
      appLayout.hidden = false;
    } catch (error) {
      if (loadAttempt !== latestLoadAttempt) return;
      loadingState.hidden = true;
      fatalError.hidden = false;
      appLayout.hidden = true;
      byId("fatal-error-message").textContent = error.message;
    }
  }

  load();
})();
