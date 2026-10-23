"""Tests for the Settings tab's groups and Keys block, with pytest-qt. Offscreen Qt, from conftest.

The keychain is a MemoryKeyring, the RunPod client never answers, and every removal confirmation is an injected fake, so
nothing touches the real credential store, the network or a dialog. FAKE_VALUE is an obviously fake value, so the leak
checks can search for it.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QAbstractButton, QLabel, QLineEdit, QPushButton, QToolButton, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import local_client
from meridian_desktop.api_keys import KEY_SERVICE, STATUS_NOT_SET, STATUS_SET, ApiKeys
from meridian_desktop.secret_store import RUNPOD_ACCOUNT, RUNPOD_SERVICE, KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import keys_section as keys_module
from meridian_desktop.ui.keys_section import ADDED_TEXT, STATUS_TEXT, KeysSection
from meridian_desktop.ui.runpod_panel import KEY_MISSING_TEXT, KEY_STORED_TEXT, RunPodPanel
from meridian_desktop.ui.settings_group import CollapsibleGroup
from meridian_desktop.ui.settings_tab import SettingsTab

FAKE_VALUE = "sk-fake-keys-ui-0001"
FAKE_VALUE_TWO = "sk-fake-keys-ui-0002"
GROUP_ORDER = (
    "settings-group-connection",
    "settings-group-local-server",
    "settings-group-keys",
    "settings-group-client-setup",
    "settings-group-diagnostics",
)
GROUP_TITLES = ("Connection", "Local server and startup", "Keys", "Client setup", "Diagnostics (logs)")
TAB_WIDTH = 1280
TAB_HEIGHT = 3200


class FakeClient:
    """A RunPod client that never answers. The probe reads 'not running', so no request is sent."""

    def probe_health(self, url, timeout):
        return local_client.HEALTH_NOT_RUNNING


def _unexpected_list(url, token):
    raise AssertionError("the hosted list must not be called in these tests")


def _no_dialog(parent, start_dir):
    raise AssertionError("the real file dialog must not open in tests")


def _tab(qtbot, settings_path, secrets, **kwargs):
    tab = SettingsTab(
        SettingsStore(settings_path),
        choose_database=_no_dialog,
        secrets=secrets,
        list_hosted=_unexpected_list,
        **kwargs,
    )
    qtbot.addWidget(tab)
    return tab


def _page_and_tab(qtbot, settings_path, secrets):
    """The main window's arrangement: the RunPod panel is the Settings page, and the tab sits in it."""
    container = QWidget()
    qtbot.addWidget(container)
    page = RunPodPanel(SettingsStore(settings_path), secrets, client=FakeClient(), parent=container)
    tab = SettingsTab(SettingsStore(settings_path), parent=page, secrets=secrets, list_hosted=_unexpected_list)
    page.layout().insertWidget(0, tab)
    return container, page, tab


def _all_text(widget) -> str:
    """Every label, button and placeholder on the widget. A leak check searches this.

    An entry box's own text is left out: the owner's typed value sits in a password box, drawn as dots, until a save clears it.
    """
    parts = [label.text() for label in widget.findChildren(QLabel)]
    parts += [button.text() for button in widget.findChildren(QPushButton)]
    parts += [line.placeholderText() for line in widget.findChildren(QLineEdit)]
    return "\n".join(parts)


def _group_names(tab) -> list[str]:
    content = tab.findChild(QWidget, "settings-content")
    layout = content.layout()
    widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
    return [widget.objectName() for widget in widgets if widget is not None]


def _seed(settings_path, backend, *, names=("OpenAI", "Anthropic"), value_for=None):
    SettingsStore(settings_path).save(Settings(key_names=tuple(names)))
    if value_for is not None:
        backend.set_password(KEY_SERVICE, "key-" + value_for.lower(), FAKE_VALUE)


# ----------------------------------------------------------- groups


def test_the_five_groups_are_in_the_requested_order(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))

    assert _group_names(tab) == list(GROUP_ORDER)
    titles = [tab.findChild(QToolButton, f"{name}-toggle").text() for name in GROUP_ORDER]
    assert tuple(titles) == GROUP_TITLES


def test_diagnostics_starts_folded_and_the_others_start_open(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))

    for name in GROUP_ORDER[:-1]:
        assert tab.findChild(CollapsibleGroup, name).is_expanded() is True, name
    assert tab.findChild(CollapsibleGroup, "settings-group-diagnostics").is_expanded() is False


def test_a_group_folds_and_unfolds_and_keeps_its_widgets(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))
    group = tab.findChild(CollapsibleGroup, "settings-group-keys")
    keys = tab.findChild(KeysSection, "keys-section")
    toggle = tab.findChild(QAbstractButton, "settings-group-keys-toggle")

    toggle.click()
    assert group.is_expanded() is False
    assert tab.findChild(KeysSection, "keys-section") is keys, "folding keeps the widgets"

    toggle.click()
    assert group.is_expanded() is True


def test_the_content_column_never_narrows_below_its_minimum_width(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))

    assert tab.findChild(QWidget, "settings-content").minimumWidth() >= 600


# ----------------------------------------------------------- RunPod entry


def test_the_runpod_card_moves_into_the_keys_group_and_the_page_stays_the_runpod_panel(qtbot, settings_path):
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    container, page, tab = _page_and_tab(qtbot, settings_path, secrets)

    assert page.objectName() == "runpod-panel", "the Settings page is still the RunPod panel"
    card = page.findChild(QWidget, "runpod-entry-card")
    assert card is not None
    assert tab.findChild(CollapsibleGroup, "settings-group-keys").isAncestorOf(card)
    assert len(container.findChildren(RunPodPanel)) == 1, "the window holds one RunPod panel"


def test_the_runpod_entry_still_saves_and_removes_its_key(qtbot, settings_path):
    backend = MemoryKeyring()
    secrets = KeyringSecretStore(backend=backend)
    _container, page, tab = _page_and_tab(qtbot, settings_path, secrets)
    key_input = tab.findChild(QLineEdit, "runpod-key-input")
    status = tab.findChild(QLabel, "runpod-key-status")
    # The panel's first keychain read holds its buttons until it answers, so wait for it before the first click.
    qtbot.waitUntil(lambda: not page.busy, timeout=5000)

    key_input.setText(FAKE_VALUE)
    tab.findChild(QPushButton, "runpod-save-key").click()
    qtbot.waitUntil(lambda: status.text() == KEY_STORED_TEXT, timeout=5000)

    assert backend.snapshot() == {(RUNPOD_SERVICE, RUNPOD_ACCOUNT): FAKE_VALUE}
    assert key_input.text() == ""
    assert FAKE_VALUE not in _all_text(tab)

    qtbot.waitUntil(lambda: not page.busy, timeout=5000)
    tab.findChild(QPushButton, "runpod-remove-key").click()
    qtbot.waitUntil(lambda: status.text() == KEY_MISSING_TEXT, timeout=5000)
    assert backend.snapshot() == {}


def test_the_keys_block_lists_runpod_once_as_a_named_card(qtbot, settings_path):
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    _container, _page, tab = _page_and_tab(qtbot, settings_path, secrets)

    heading = tab.findChild(QLabel, "keys-runpod-name")
    assert heading is not None and heading.text() == "RunPod"
    assert tab.findChild(QWidget, "keys-entry-runpod") is not None


# ----------------------------------------------------------- named keys


def test_a_named_key_is_added_and_shows_set_without_its_value(qtbot, settings_path):
    backend = MemoryKeyring()
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=backend))

    tab.findChild(QLineEdit, "keys-new-name").setText("OpenAI")
    tab.findChild(QLineEdit, "keys-new-value").setText(FAKE_VALUE)
    tab.findChild(QPushButton, "keys-add").click()

    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE}
    assert tab.findChild(QLabel, "keys-entry-openai-name").text() == "OpenAI"
    assert tab.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]
    assert tab.findChild(QLineEdit, "keys-new-value").text() == ""
    assert tab.findChild(QLabel, "keys-message").text() == ADDED_TEXT.format(name="OpenAI")
    assert SettingsStore(settings_path).load().settings.key_names == ("OpenAI",)
    assert FAKE_VALUE not in _all_text(tab)


def test_a_refused_name_is_shown_inline_and_nothing_is_written(qtbot, settings_path):
    backend = MemoryKeyring()
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=backend))

    tab.findChild(QLineEdit, "keys-new-name").setText("bad/name")
    tab.findChild(QLineEdit, "keys-new-value").setText(FAKE_VALUE)
    tab.findChild(QPushButton, "keys-add").click()

    message = tab.findChild(QLabel, "keys-message")
    assert not message.isHidden(), "the refusal is shown"
    assert "letters, digits, spaces" in message.text()
    assert backend.snapshot() == {}
    assert not settings_path.exists()
    assert FAKE_VALUE not in _all_text(tab)


def test_saved_names_show_their_status_from_the_keychain(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic"), value_for="OpenAI")

    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=backend))

    assert tab.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]
    assert tab.findChild(QLabel, "keys-entry-anthropic-status").text() == STATUS_TEXT[STATUS_NOT_SET]
    assert FAKE_VALUE not in _all_text(tab)


def test_replace_changes_only_the_value_and_remove_asks_first(qtbot, settings_path, monkeypatch):
    backend = MemoryKeyring()
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    tab.findChild(QLineEdit, "keys-new-name").setText("OpenAI")
    tab.findChild(QLineEdit, "keys-new-value").setText(FAKE_VALUE)
    tab.findChild(QPushButton, "keys-add").click()

    # Replace: the box is cleared, the listing stays, and only the value changes.
    tab.findChild(QLineEdit, "keys-entry-openai-value").setText(FAKE_VALUE_TWO)
    tab.findChild(QPushButton, "keys-entry-openai-save").click()
    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE_TWO}
    assert tab.findChild(QLineEdit, "keys-entry-openai-value").text() == ""
    assert tab.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]

    # Remove, declined: the value and the name are kept.
    asked = []
    monkeypatch.setattr(keys_module, "_ask_remove", lambda parent, title, text: asked.append(text) or False)
    tab.findChild(QPushButton, "keys-entry-openai-remove").click()
    assert len(asked) == 1 and "OpenAI" in asked[0], "the owner is asked first, and the name is in the question"
    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE_TWO}
    assert tab.findChild(QLabel, "keys-message").text() == "Cancelled. Nothing was removed."

    # Remove, confirmed: the value goes, the name leaves the list, and its card is gone.
    monkeypatch.setattr(keys_module, "_ask_remove", lambda parent, title, text: True)
    tab.findChild(QPushButton, "keys-entry-openai-remove").click()
    assert backend.snapshot() == {}
    # The card is removed when the event loop runs its deferred delete, so wait for that.
    qtbot.waitUntil(lambda: tab.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)
    assert SettingsStore(settings_path).load().settings.key_names == ()


def test_no_value_is_ever_shown_on_the_tab_after_a_save_or_a_refresh(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI",), value_for="OpenAI")
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=backend))

    assert FAKE_VALUE not in _all_text(tab)
    tab.refresh()
    assert FAKE_VALUE not in _all_text(tab)
    if settings_path.exists():
        assert FAKE_VALUE.encode() not in settings_path.read_bytes(), "the settings file holds names, never values"


def test_the_keys_section_on_its_own_has_no_runpod_card_and_lists_custom_names(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI",), value_for="OpenAI")
    keys = ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend))
    section = KeysSection(keys)
    qtbot.addWidget(section)

    assert section.findChild(QWidget, "keys-entry-runpod") is None, "no RunPod card without a panel to host"
    assert section.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]


# ----------------------------------------------------------- no clipping at 1280 px


def _expand_all(tab) -> None:
    for name in GROUP_ORDER:
        tab.findChild(CollapsibleGroup, name).set_expanded(True)


def _fully_inside(widget, tab) -> bool:
    origin = widget.mapTo(tab, QPoint(0, 0))
    return origin.x() >= 0 and origin.x() + widget.width() <= tab.width()


def _show_at_1280(qtbot, container, page, tab) -> None:
    """Show the page as the main window does, sized 1280 wide and tall enough that no group scrolls out of sight."""
    page.resize(TAB_WIDTH, TAB_HEIGHT)
    container.show()
    qtbot.waitExposed(container)
    qtbot.waitUntil(lambda: tab.findChild(QWidget, "settings-content").width() >= 600, timeout=5000)
    qtbot.wait(30)


def test_no_label_or_button_is_clipped_at_1280_px(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic"), value_for="OpenAI")
    secrets = KeyringSecretStore(backend=backend)
    container, page, tab = _page_and_tab(qtbot, settings_path, secrets)
    _expand_all(tab)
    _show_at_1280(qtbot, container, page, tab)

    labels = [label for label in tab.findChildren(QLabel) if label.isVisibleTo(tab) and label.text()]
    assert len(labels) >= 20, "the walk reaches the real labels on every group"
    for label in labels:
        if label.wordWrap():
            assert label.height() >= label.heightForWidth(label.width()) - 1, label.objectName() or label.text()
        else:
            assert label.width() >= label.sizeHint().width() - 1, label.objectName() or label.text()
        assert _fully_inside(label, tab), label.objectName() or label.text()

    buttons = [button for button in tab.findChildren(QPushButton) if button.isVisibleTo(tab)]
    assert len(buttons) >= 15
    for button in buttons:
        assert button.width() >= button.minimumSizeHint().width() - 1, button.objectName()
        assert button.height() >= button.minimumSizeHint().height() - 1, button.objectName()
        assert _fully_inside(button, tab), button.objectName()


def test_client_setup_apply_remove_and_the_check_result_are_fully_visible_at_1280_px(qtbot, settings_path):
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    container, page, tab = _page_and_tab(qtbot, settings_path, secrets)
    _expand_all(tab)
    result = tab.findChild(QLabel, "client-setup-result")
    result.setText(
        "Written: C:\\Users\\example\\.claude\\settings.json. Backup saved at: "
        "C:\\Users\\example\\.claude\\settings.json.bak. Restart Claude Code to load the change."
    )
    result.setVisible(True)
    _show_at_1280(qtbot, container, page, tab)

    actions = [
        button
        for button in tab.findChild(CollapsibleGroup, "settings-group-client-setup").findChildren(QPushButton)
        if button.objectName().endswith(("-apply", "-remove"))
    ]
    assert len(actions) >= 4, "each client has its Apply and Remove"
    for button in actions:
        assert button.height() >= button.minimumSizeHint().height() - 1, button.objectName()
        assert _fully_inside(button, tab), button.objectName()
    assert result.height() >= result.heightForWidth(result.width()) - 1, "the check result is not cut off"
    for caption in ("client-setup-checkout-label", "client-setup-python-label"):
        label = tab.findChild(QLabel, caption)
        assert label.wordWrap() is False, caption
        assert label.width() >= label.sizeHint().width() - 1, caption
