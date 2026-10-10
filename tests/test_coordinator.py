"""Coordinator tests against the 2-stage TelegramBundle wire format (T21e)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.ecotherm_wolf.coordinator import EcothermWolfCoordinator
from custom_components.ecotherm_wolf.definitions.catalog import (
    Catalog,
    DeviceDefinition,
    ParameterDefinition,
)
from custom_components.ecotherm_wolf.definitions.codecs import Int16Codec, ScaledCodec
from custom_components.ecotherm_wolf.definitions.converters import NumericConverter32
from custom_components.ecotherm_wolf.ism7.protocol import (
    InfonumberReadResp,
    InfonumberWriteResp,
    TelegramBundleReq,
    TelegramBundleResp,
)


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(devices={
        "CHA-10": DeviceDefinition(
            device_type="CHA-10", display_name="CHA",
            parameters={
                0x0080: ParameterDefinition(
                    property_id=0x0080,
                    name="vorlauftemp",
                    display_name="Vorlauftemperatur",
                    codec=ScaledCodec(base=Int16Codec(unit="°C"), scale=10),
                ),
            },
        )
    })


def make_client_mock(*, pull_resp: TelegramBundleResp | None = None) -> MagicMock:
    client = MagicMock()
    client.connected = True
    client.send = AsyncMock()
    # connect/disconnect are awaited by the pause/resume and paused-setup
    # paths. A bare MagicMock attribute is not awaitable -- the coordinator's
    # best-effort try/except would silently swallow the TypeError and the
    # test's assert_awaited() would explode instead of failing cleanly.
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.force_disconnect = AsyncMock()
    # Default pull response: empty (no telegrams). Tests that want a real
    # snapshot pass `pull_resp`.
    client.request = AsyncMock(
        return_value=pull_resp or TelegramBundleResp(bundle_id="1", state="ok"),
    )
    client.on_update = MagicMock()
    client.on_reconnect = MagicMock()
    return client


def _device_map_with_bus() -> dict[int, tuple[str, str]]:
    """Legacy 2-tuple device map -- write address defaults to == read.

    ``_normalise_device_map`` expands this to
    ``(device_type, read, read)``; using it keeps the bulk of the
    coordinator tests exercising the write == read case unchanged.
    """
    return {1: ("CHA-10", "0;0;0;1")}


def _device_map_split_rw() -> dict[int, tuple[str, str, str]]:
    """3-tuple device map where the write address differs from the read.

    Mirrors a real Wolf BM-2 / CHA: reads come back on one bus address,
    writes must be sent to another.
    """
    return {1: ("CHA-10", "0;0;0;1", "0;0;0;9")}


@pytest.mark.asyncio
async def test_initial_pull_then_subscribe_sent(
    hass: HomeAssistant, catalog,
) -> None:
    """Init interleaves pull then subscribe per bundle, both via ``request``.

    zivillian's pattern: pull a (single-bus) bundle, then push-subscribe the
    *same* bundle before advancing. Both stages are ``request``-gated now --
    the subscribe moved off ``send`` so we wait for each ack and never flood
    the ISM7's eBus poller.
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()
    # Stage 1: pull bundle via client.request
    pull = client.request.await_args_list[0].args[0]
    assert isinstance(pull, TelegramBundleReq)
    assert pull.bundle_type == "pull"
    assert pull.abort_on_error is False
    assert pull.bundle_id == "1"
    assert pull.info_reads[0].info_number == 0x0080
    assert pull.info_reads[0].seq == "1"
    # Stage 2: push bundle, also via client.request (awaited ack, throttled)
    push = client.request.await_args_list[1].args[0]
    assert isinstance(push, TelegramBundleReq)
    assert push.bundle_type == "push"
    assert push.bundle_id == "2"
    assert push.info_reads[0].info_number == 0x0080
    assert push.info_reads[0].interval == 60
    # The subscribe is no longer fired via the un-awaited send().
    assert client.send.await_count == 0


@pytest.mark.asyncio
async def test_value_update_lands_in_state(hass: HomeAssistant, catalog) -> None:
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()

    callback = client.on_update.call_args.args[0]
    callback(TelegramBundleResp(
        bundle_id="push_initial", state="ok",
        read_telegrams=[
            InfonumberReadResp(
                info_number=0x0080, bus_address="0;0;0;1",
                state="ok", db_low="F5", db_high="00",
            ),
        ],
    ))
    await asyncio.sleep(0)
    assert coord.data[(1, 0x0080)] == 24.5


@pytest.mark.asyncio
async def test_service_number_routes_shared_telegram(hass: HomeAssistant) -> None:
    """Two params share TelegramNr 11 under different eBus services.

    The CWL exposes the same TelegramNr under service 4022 AND 4050, so a
    push frame carries telegram 11 twice -- once per ``snr``.
    ``_process_telegrams`` keys on (info_number, service_number) so neither
    half is dropped, and ``_decode_param`` routes each ``irs`` to the
    converter whose ServiceReadNumber matches. Regression for the CWL
    0xFFFF bug (the integration used to drop ``snr`` entirely, so the ISM7
    answered 0xFFFF for every non-default-service telegram).
    """
    from custom_components.ecotherm_wolf.definitions.converters import (
        NumericConverter16,
    )

    def _conv(ctid: int, service: int) -> NumericConverter16:
        c = NumericConverter16(ctid, telegram_nr=11, type_code="US")
        c.service_read_number = service
        return c

    catalog = Catalog(devices={
        "CWL": DeviceDefinition(
            device_type="CWL", display_name="CWL",
            parameters={
                0x0001: ParameterDefinition(
                    property_id=0x0001, name="luftstufe",
                    display_name="Lüftungsstufe", codec=Int16Codec(),
                    info_numbers=[11], converter=_conv(1, 4022),
                ),
                0x0002: ParameterDefinition(
                    property_id=0x0002, name="stufe0",
                    display_name="Stufe 0", codec=Int16Codec(),
                    info_numbers=[11], converter=_conv(2, 4050),
                ),
            },
        ),
    })
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map={1: ("CWL", "0x7C", "0x7C")},
        selected=[(1, 0x0001), (1, 0x0002)],
    )
    await coord.async_setup()

    # build_bundles must have sent both reads, each with its own snr.
    pull = client.request.await_args_list[0].args[0]
    sent = {(ir.info_number, ir.service_number) for ir in pull.info_reads}
    assert sent == {(11, 4022), (11, 4050)}

    callback = client.on_update.call_args.args[0]
    callback(TelegramBundleResp(
        bundle_id="push", state="ok",
        read_telegrams=[
            InfonumberReadResp(
                info_number=11, bus_address="0x7C", state="ok",
                db_low="03", db_high="00", service_number=4022,
            ),
            InfonumberReadResp(
                info_number=11, bus_address="0x7C", state="ok",
                db_low="2A", db_high="00", service_number=4050,
            ),
        ],
    ))
    await asyncio.sleep(0)
    # 4022 telegram -> param 1 (0x0003); 4050 telegram -> param 2 (0x002A).
    assert coord.data[(1, 0x0001)] == 3
    assert coord.data[(1, 0x0002)] == 42


@pytest.mark.asyncio
async def test_apply_selection_diff_unsubscribes_removed(
    hass: HomeAssistant, catalog,
) -> None:
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()
    client.send.reset_mock()

    await coord.apply_selection([])
    sent = [c.args[0] for c in client.send.await_args_list]
    # Should have a remove bundle for (1, 0x0080).
    remove_bundles = [
        s for s in sent
        if isinstance(s, TelegramBundleReq) and s.bundle_type == "remove"
    ]
    assert len(remove_bundles) == 1
    assert remove_bundles[0].info_reads[0].info_number == 0x0080


@pytest.mark.asyncio
async def test_apply_selection_diff_subscribes_added(
    hass: HomeAssistant, catalog,
) -> None:
    """Adding a parameter triggers a push-bundle for the new entry."""
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()
    # Drop the init-flow requests so only the apply_selection ones remain.
    client.request.reset_mock()

    await coord.apply_selection([(1, 0x0080), (1, 0x010C)])
    # The subscribe for the added key goes through ``request`` (throttled,
    # awaited ack) -- same path as the init flow, no un-awaited ``send``.
    sent = [c.args[0] for c in client.request.await_args_list]
    push_bundles = [
        s for s in sent
        if isinstance(s, TelegramBundleReq) and s.bundle_type == "push"
    ]
    assert len(push_bundles) == 1
    assert push_bundles[0].info_reads[0].info_number == 0x010C


@pytest.mark.asyncio
async def test_write_encodes_and_sends(hass: HomeAssistant, catalog) -> None:
    """T21e: write goes through the codec/converter and lands as iwr.

    Verify-read is dropped in T21e -- there's just one request (the write).
    """
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    write_resp = TelegramBundleResp(
        bundle_id="3", state="ok",
        write_telegrams=[InfonumberWriteResp(
            info_number=0x010C, bus_address="0;0;0;1",
            state="ok", db_low="14", db_high="00",
        )],
    )
    # Init interleaves pull + subscribe per bundle (both via ``request``);
    # the write is then the 3rd request.
    client = make_client_mock()
    client.request = AsyncMock(side_effect=[
        TelegramBundleResp(bundle_id="1", state="ok"),  # initial pull
        TelegramBundleResp(bundle_id="2", state="ok"),  # subscribe ack
        write_resp,  # the write itself
    ])
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x010C)],
    )
    await coord.async_setup()

    await coord.async_write(device_id=1, property_id=0x010C, value=20)
    write = client.request.await_args_list[2].args[0]
    assert isinstance(write, TelegramBundleReq)
    assert write.bundle_type == "write"
    assert write.abort_on_error is True
    # Int16 encode of 20 = 0x0014 -> dl="0x14", dh="0x00". The 0x prefix
    # is required -- the ISM7 drops the connection on bare hex in iwr.
    assert write.info_writes[0].db_low == "0x14"
    assert write.info_writes[0].db_high == "0x00"
    # zivillian sends iwr telegrams with an empty seq attribute.
    assert write.info_writes[0].seq == ""
    assert coord.data[(1, 0x010C)] == 20


@pytest.mark.asyncio
async def test_write_timeout_surfaces_to_caller(
    hass: HomeAssistant, catalog,
) -> None:
    """W11: a write whose ack never arrives surfaces as a clean error.

    ``client.request`` raises the bare builtin ``TimeoutError`` (from
    ``asyncio.wait_for``) when the gateway doesn't ACK in time. That is
    *not* an ``Ism7Error``, so ``async_write`` has to catch it explicitly
    and wrap it into a ``HomeAssistantError`` -- left unwrapped HA renders
    it as a useless "unknown error" toast (the live bug hit on
    the MK_BM-2 Programmwahl select).
    """
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    client = make_client_mock()
    # Init now issues pull + subscribe (both ``request``); the write is 3rd.
    client.request = AsyncMock(side_effect=[
        TelegramBundleResp(bundle_id="1", state="ok"),  # initial pull
        TelegramBundleResp(bundle_id="2", state="ok"),  # subscribe ack
        TimeoutError("server did not ack the write"),
    ])
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x010C)],
    )
    await coord.async_setup()

    with pytest.raises(HomeAssistantError, match="Zeitüberschreitung"):
        await coord.async_write(device_id=1, property_id=0x010C, value=20)


@pytest.mark.asyncio
async def test_write_rejected_ack_state_has_clear_message(
    hass: HomeAssistant, catalog,
) -> None:
    """A non-OK iac state gets a human-readable reason, not "ack state=to".

    The ISM7 answers an iac with OK / TO / BU / ER. ``TO`` (eBus timeout)
    is the common one -- the gateway forwarded the write but the module
    never acknowledged it, e.g. the CWL's "Luftdurchsatz Stufe 3", which
    is only settable on the unit itself (zivillian sees the identical TO).
    The raw "write rejected: ack state=to" toast told the user nothing, so
    the message has to spell out what TO means.
    """
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    client = make_client_mock()
    client.request = AsyncMock(side_effect=[
        TelegramBundleResp(bundle_id="1", state="ok"),  # initial pull
        TelegramBundleResp(bundle_id="2", state="ok"),  # subscribe ack
        TelegramBundleResp(  # write: gateway accepted, module didn't ack
            bundle_id="3", state="ok",
            write_telegrams=[InfonumberWriteResp(
                info_number=0x010C, bus_address="0;0;0;1",
                state="to", db_low="0x14", db_high="0x00",
            )],
        ),
    ])
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x010C)],
    )
    await coord.async_setup()

    with pytest.raises(HomeAssistantError, match="eBus-Timeout"):
        await coord.async_write(device_id=1, property_id=0x010C, value=20)


@pytest.mark.asyncio
async def test_multi_telegram_push_decoded_when_both_halves_arrive(
    hass: HomeAssistant, catalog,
) -> None:
    """W11: 32-bit counter spread across two info_numbers in one push frame.

    A multi-telegram parameter only decodes when ALL its info_numbers are
    in the same response (push frame). This test pins the contract: feed
    two telegrams in one bundle, expect a single decoded value.
    """
    from custom_components.ecotherm_wolf.definitions.converters import (
        NumericConverter32,
    )

    # Override the existing parameter with a 32-bit converter.
    cat32 = Catalog(devices={
        "CHA-10": DeviceDefinition(
            device_type="CHA-10", display_name="CHA",
            parameters={
                0x0080: ParameterDefinition(
                    property_id=0x0080,
                    name="counter",
                    display_name="Verdichterstunden",
                    codec=ScaledCodec(base=Int16Codec(unit="h"), scale=1),
                    converter=NumericConverter32(
                        ctid=0x0080,
                        telegram_nr_low=10,
                        telegram_nr_high=11,
                        type_code="UINT32",
                    ),
                ),
            },
        )
    })
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=cat32,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()

    callback = client.on_update.call_args.args[0]
    # One push frame carries both halves of the 32-bit counter (low+high).
    callback(TelegramBundleResp(
        bundle_id="p1", state="ok",
        read_telegrams=[
            InfonumberReadResp(
                info_number=10, bus_address="0;0;0;1",
                state="ok", db_low="01", db_high="00",  # lo word
            ),
            InfonumberReadResp(
                info_number=11, bus_address="0;0;0;1",
                state="ok", db_low="00", db_high="00",  # hi word = 0
            ),
        ],
    ))
    await asyncio.sleep(0)
    # value = (hi << 16) | lo = 0x00000001 = 1
    assert coord.data[(1, 0x0080)] == 1


@pytest.mark.asyncio
async def test_initial_pull_continues_after_per_bundle_timeout(
    hass: HomeAssistant, catalog,
) -> None:
    """W11: a timed-out pull bundle must not abort the other bundles.

    Two distinct devices on two bus addresses => two single-bus bundles.
    Init interleaves pull+subscribe per bundle, so the request order is
    pull#1, subscribe#1, pull#2, subscribe#2 (bundles are sorted by bus
    address, so ``0;0;0;1`` comes first). pull#1 raises TimeoutError;
    pull#2 succeeds and its value lands in coord.data. Pins the contract
    that one slow bundle doesn't black-hole the whole snapshot.
    """
    catalog.devices["CHA-10"].parameters[0x0080] = ParameterDefinition(
        property_id=0x0080, name="vorlauf", display_name="Vorlauf",
        codec=ScaledCodec(base=Int16Codec(unit="°C"), scale=10),
    )
    # Add a second device with a parameter on a different bus_address so the
    # bundle-builder produces two bundles.
    catalog.devices["BM-2"] = DeviceDefinition(
        device_type="BM-2", display_name="BM-2",
        parameters={
            0x010C: ParameterDefinition(
                property_id=0x010C, name="raumtemp", display_name="Raum",
                codec=ScaledCodec(base=Int16Codec(unit="°C"), scale=10),
            ),
        },
    )
    device_map = {1: ("CHA-10", "0;0;0;1"), 2: ("BM-2", "0;0;0;3")}
    pull2_resp = TelegramBundleResp(
        bundle_id="3", state="ok",
        read_telegrams=[
            InfonumberReadResp(
                info_number=0x010C, bus_address="0;0;0;3",
                state="ok", db_low="F5", db_high="00",
            ),
        ],
    )
    client = make_client_mock()
    # pull#1 (bus 0;0;0;1) times out; subscribe#1, pull#2, subscribe#2 ok.
    client.request = AsyncMock(side_effect=[
        TimeoutError("first bundle times out"),          # pull bundle #1
        TelegramBundleResp(bundle_id="2", state="ok"),   # subscribe #1
        pull2_resp,                                      # pull bundle #2
        TelegramBundleResp(bundle_id="4", state="ok"),   # subscribe #2
    ])
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=device_map,
        selected=[(1, 0x0080), (2, 0x010C)],
    )
    await coord.async_setup()

    # Bundle 1 lost; bundle 2's value is in the cache.
    assert (2, 0x010C) in coord.data
    assert coord.data[(2, 0x010C)] == 24.5


@pytest.mark.asyncio
async def test_set_catalog_drops_vanished_param_from_selection(
    hass: HomeAssistant, catalog,
) -> None:
    """W5+W11: ``set_catalog`` prunes selections that no longer exist.

    Build coordinator with selection {0x0080}. Then pass a new catalog
    where 0x0080 is missing -> coordinator drops that key from
    ``_selected`` and from ``data`` cache, and re-emits unsubscribe via
    apply_selection.
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()
    coord.data[(1, 0x0080)] = 24.5  # simulate prior push

    # New catalog: same device but the parameter is gone.
    new_cat = Catalog(devices={
        "CHA-10": DeviceDefinition(
            device_type="CHA-10", display_name="CHA",
            parameters={},  # 0x0080 vanished
        )
    })
    await coord.set_catalog(new_cat)

    assert (1, 0x0080) not in coord.data
    assert coord.selected == []


@pytest.mark.asyncio
async def test_set_heartbeat_seconds_resubscribes(
    hass: HomeAssistant, catalog,
) -> None:
    """Task 3.1: changing the heartbeat must trigger a fresh ``push`` bundle.

    The ISM7 keeps the interval from the most recent subscribe, so just
    updating the in-memory value would leave the gateway pushing at the
    old rate. We assert that ``set_heartbeat_seconds`` re-sends a push
    bundle whose Intervall reflects the new value -- via ``request`` now
    (the subscribe path is throttled / awaits each ack).
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
        heartbeat_seconds=60,
    )
    await coord.async_setup()
    client.request.reset_mock()

    await coord.set_heartbeat_seconds(120)
    sent = [c.args[0] for c in client.request.await_args_list]
    push_bundles = [
        s for s in sent
        if isinstance(s, TelegramBundleReq) and s.bundle_type == "push"
    ]
    assert len(push_bundles) == 1
    assert push_bundles[0].info_reads[0].interval == 120
    assert coord.heartbeat_seconds == 120


@pytest.mark.asyncio
async def test_set_heartbeat_seconds_noop_when_unchanged(
    hass: HomeAssistant, catalog,
) -> None:
    """Setting the same value as already configured is a no-op (no re-subscribe)."""
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
        heartbeat_seconds=60,
    )
    await coord.async_setup()
    client.send.reset_mock()
    client.request.reset_mock()

    await coord.set_heartbeat_seconds(60)
    # No re-subscribe on either path.
    assert client.send.await_count == 0
    assert client.request.await_count == 0


@pytest.mark.asyncio
async def test_set_active_false_disconnects_and_stops_tasks(
    hass: HomeAssistant, catalog,
) -> None:
    """Task 3.2: turning the Active switch OFF disconnects the client
    and cancels the keepalive + watchdog tasks."""
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()
    assert coord.is_active is True

    await coord.async_set_active(False)
    assert coord.is_active is False
    client.disconnect.assert_awaited()
    # Both background tasks should be cleared.
    assert coord._watchdog_task is None
    assert coord._keepalive_task is None


@pytest.mark.asyncio
async def test_set_active_true_after_false_reconnects(
    hass: HomeAssistant, catalog,
) -> None:
    """Toggling the Active switch back ON reconnects, re-pulls, re-subscribes."""
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()
    await coord.async_set_active(False)

    client.connect.reset_mock()
    client.send.reset_mock()
    client.request.reset_mock()

    await coord.async_set_active(True)
    assert coord.is_active is True
    client.connect.assert_awaited()
    # Re-init runs the interleaved pull+subscribe -- both via ``request``.
    # 1 selected param -> 1 single-bus bundle -> pull + subscribe = 2 reqs.
    assert client.request.await_count >= 2
    reqs = [c.args[0] for c in client.request.await_args_list]
    push_bundles = [
        r for r in reqs
        if isinstance(r, TelegramBundleReq) and r.bundle_type == "push"
    ]
    assert len(push_bundles) == 1
    # The subscribe is no longer fired via the un-awaited send().
    assert client.send.await_count == 0


@pytest.mark.asyncio
async def test_set_active_toggle_idempotent(
    hass: HomeAssistant, catalog,
) -> None:
    """Rapid off/on/off/on must not leak tasks or double-connect."""
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()

    for desired in (False, True, False, True):
        await coord.async_set_active(desired)
        assert coord.is_active is desired
    # Setting again to the same value short-circuits.
    pre_connect = client.connect.await_count
    pre_disconnect = client.disconnect.await_count
    await coord.async_set_active(True)
    await coord.async_set_active(True)
    assert client.connect.await_count == pre_connect
    assert client.disconnect.await_count == pre_disconnect


@pytest.mark.asyncio
async def test_paused_write_raises_write_rejected(
    hass: HomeAssistant, catalog,
) -> None:
    """While the integration is paused, writes fail with a clear error.

    ``_async_write_inner`` raises ``WriteRejected``; ``async_write`` wraps
    every ``Ism7Error`` into a ``HomeAssistantError`` so HA shows a clean
    toast instead of an HTTP 500. The original "paused" message survives.
    """
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x010C)],
    )
    await coord.async_setup()

    await coord.async_set_active(False)
    with pytest.raises(HomeAssistantError, match="pausiert"):
        await coord.async_write(device_id=1, property_id=0x010C, value=20)


@pytest.mark.asyncio
async def test_write_to_unknown_device_id_raises_clean_error(
    hass: HomeAssistant, catalog,
) -> None:
    """A vanished device id surfaces as a clean toast, not a KeyError.

    The device map can shrink at runtime (rediscovery, edited entry). A
    write against a no-longer-mapped id used to escape as a bare KeyError
    from ``device_type_for`` -- HA rendered that as "unknown error".
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()

    with pytest.raises(HomeAssistantError, match="unbekanntes Gerät"):
        await coord.async_write(device_id=99, property_id=0x0080, value=20)


@pytest.mark.asyncio
async def test_write_to_unknown_parameter_raises_clean_error(
    hass: HomeAssistant, catalog,
) -> None:
    """A parameter that vanished from the catalog surfaces as a clean toast.

    ``reload_definitions`` can swap in a catalog that no longer carries a
    parameter whose entity still exists. ``get_parameter`` raises
    ``UnknownParameterError`` (a KeyError subclass) -- left unwrapped HA
    rendered that as "unknown error".
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()

    with pytest.raises(HomeAssistantError, match="nicht mehr im"):
        await coord.async_write(device_id=1, property_id=0x9999, value=20)
    # The failed write feeds the Health sensor's rolling window.
    assert coord.write_errors_count_24h == 1


@pytest.mark.asyncio
async def test_failed_write_feeds_write_errors_24h(
    hass: HomeAssistant, catalog,
) -> None:
    """Every failed write stamps the rolling 24h window (Health sensor).

    Regression (1.0.6): the "Write Errors 24h" sensor was never fed -- the
    counter could not leave 0 no matter how many writes failed.
    """
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    client = make_client_mock()
    client.request = AsyncMock(side_effect=[
        TelegramBundleResp(bundle_id="1", state="ok"),  # initial pull
        TelegramBundleResp(bundle_id="2", state="ok"),  # subscribe ack
        TimeoutError("no ack"),                         # write 1 -> timeout
        TelegramBundleResp(bundle_id="4", state="er"),  # write 2 -> rejected
    ])
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x010C)],
    )
    await coord.async_setup()

    assert coord.write_errors_count_24h == 0
    with pytest.raises(HomeAssistantError):
        await coord.async_write(device_id=1, property_id=0x010C, value=20)
    assert coord.write_errors_count_24h == 1
    with pytest.raises(HomeAssistantError):
        await coord.async_write(device_id=1, property_id=0x010C, value=20)
    assert coord.write_errors_count_24h == 2


def _add_32bit_counter(catalog: Catalog, ptid: int = 0x9000) -> None:
    """Add a Verdichterstarts-style 32-bit counter (telegrams 550+551)."""
    conv = NumericConverter32(
        ptid, telegram_nr_low=550, telegram_nr_high=551, type_code="UINT32",
    )
    catalog.devices["CHA-10"].parameters[ptid] = ParameterDefinition(
        property_id=ptid,
        name="verdichterstarts",
        display_name="Verdichterstarts",
        codec=Int16Codec(),
        info_numbers=[550, 551],
        converter=conv,
    )


def _push_frame(*entries: tuple[int, str, str]) -> TelegramBundleResp:
    """Build an unsolicited push frame from ``(info_number, dl, dh)`` rows."""
    return TelegramBundleResp(
        bundle_id="push", state="ok",
        read_telegrams=[
            InfonumberReadResp(
                info_number=nr, bus_address="0;0;0;1",
                state="ok", db_low=dl, db_high=dh,
            )
            for nr, dl, dh in entries
        ],
    )


@pytest.mark.asyncio
async def test_multi_telegram_value_updates_from_partial_push_frame(
    hass: HomeAssistant, catalog,
) -> None:
    """Regression (2.0.0): 32-bit counters must not freeze on partial pushes.

    The ISM7's push frames usually carry only the telegram whose value
    changed -- for a 32-bit counter that is the low word alone, while the
    high word was last seen frames earlier. The old frame-local decode
    required BOTH halves in the same frame, so ``Verdichterstarts`` only
    ever advanced on the reconnect pull snapshot (the live bug). The
    telegram cache combines the fresh low word with the cached high word.
    """
    _add_32bit_counter(catalog)
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x9000)],
    )
    await coord.async_setup()

    # Full snapshot (as the initial pull delivers): 595 == 0x0253.
    coord._on_update(_push_frame((550, "53", "02"), (551, "00", "00")))
    assert coord.data[(1, 0x9000)] == 595

    # Compressor starts -> the ISM7 pushes ONLY the changed low word
    # (596 == 0x0254). Previously this froze the value at 595.
    coord._on_update(_push_frame((550, "54", "02")))
    assert coord.data[(1, 0x9000)] == 596

    # Another partial push, low word only: 597 == 0x0255.
    coord._on_update(_push_frame((550, "55", "02")))
    assert coord.data[(1, 0x9000)] == 597


@pytest.mark.asyncio
async def test_telegram_cache_cleared_on_resubscribe(
    hass: HomeAssistant, catalog,
) -> None:
    """The telegram cache is a per-connection view -- reconnect clears it.

    After ``_resubscribe`` (reconnect) the previous session's halves are
    gone: a lone low-word push right after the clear must NOT decode
    against pre-reconnect data. The entity keeps its last value (by
    design) until the fresh full snapshot arrives.
    """
    _add_32bit_counter(catalog)
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x9000)],
    )
    await coord.async_setup()

    coord._on_update(_push_frame((550, "53", "02"), (551, "00", "00")))
    assert coord.data[(1, 0x9000)] == 595
    assert coord._telegram_cache  # populated

    await coord._resubscribe()
    assert coord._telegram_cache == {}

    # Lone low word after the session boundary: no decode (high missing),
    # value holds at the last known reading.
    coord._on_update(_push_frame((550, "54", "02")))
    assert coord.data[(1, 0x9000)] == 595

    # The full snapshot (re-delivered by the fresh session) updates again.
    coord._on_update(_push_frame((550, "54", "02"), (551, "00", "00")))
    assert coord.data[(1, 0x9000)] == 596


@pytest.mark.asyncio
async def test_setup_with_persisted_inactive_does_not_pull(
    hass: HomeAssistant, catalog,
) -> None:
    """``is_active=False`` at construction time means async_setup closes the
    socket and skips the initial pull / subscribe (matches the user's
    intent across restarts).
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x0080)],
        is_active=False,
    )
    await coord.async_setup()
    # No pull, no subscribe.
    assert client.request.await_count == 0
    assert client.send.await_count == 0
    # And the client was disconnected (from the socket __init__.py opened).
    client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_select_decodes_enum_via_converter(
    hass: HomeAssistant,
) -> None:
    """Task 2: Wolf ``ListParameterDescriptor`` => EnumCodec + NumericConverter16.

    A push frame carrying a raw int 3 must land in ``data`` as the
    matching members[3] string, not as 3. This is the regression
    Observed live where ``select.bm_2_programmwahl_luftung``
    showed ``unknown`` while the underlying int sensor showed ``3``.
    """
    from custom_components.ecotherm_wolf.definitions.codecs import EnumCodec
    from custom_components.ecotherm_wolf.definitions.converters import (
        NumericConverter16,
    )

    enum_codec = EnumCodec(
        members={0: "Auto", 1: "Reduzierte Lüftung",
                 2: "Nennlüftung", 3: "Feuchteschutz"},
        writable=True,
    )
    cat = Catalog(devices={
        "BM-2": DeviceDefinition(
            device_type="BM-2", display_name="BM-2",
            parameters={
                0x1234: ParameterDefinition(
                    property_id=0x1234,
                    name="lueftung",
                    display_name="Programmwahl Lüftung",
                    codec=enum_codec,
                    converter=NumericConverter16(
                        0x1234, telegram_nr=0x1234, type_code="US",
                    ),
                ),
            },
        )
    })
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=cat,
        device_map={1: ("BM-2", "0;0;0;1")},
        selected=[(1, 0x1234)],
    )
    await coord.async_setup()

    callback = client.on_update.call_args.args[0]
    # Raw word = 3 -> low=3, high=0
    callback(TelegramBundleResp(
        bundle_id="p1", state="ok",
        read_telegrams=[
            InfonumberReadResp(
                info_number=0x1234, bus_address="0;0;0;1",
                state="ok", db_low="03", db_high="00",
            ),
        ],
    ))
    await asyncio.sleep(0)
    assert coord.data[(1, 0x1234)] == "Feuchteschutz"


@pytest.mark.asyncio
async def test_write_no_verify_read(hass: HomeAssistant, catalog) -> None:
    """T21e drops verify-read; the write itself is a single request.

    Init issues a pull + a subscribe (both ``request``), so the write is
    the 3rd request -- and there is no 4th (no verify-read).
    """
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    write_resp = TelegramBundleResp(
        bundle_id="3", state="ok",
        write_telegrams=[InfonumberWriteResp(
            info_number=0x010C, bus_address="0;0;0;1",
            state="ok", db_low="14", db_high="00",
        )],
    )
    client = make_client_mock()
    client.request = AsyncMock(side_effect=[
        TelegramBundleResp(bundle_id="1", state="ok"),  # initial pull
        TelegramBundleResp(bundle_id="2", state="ok"),  # subscribe ack
        write_resp,  # write
    ])
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),
        selected=[(1, 0x010C)],
    )
    await coord.async_setup()

    await coord.async_write(device_id=1, property_id=0x010C, value=20)
    # Pull (1) + subscribe (1) + write (1) = 3 requests total. No verify-read.
    assert client.request.await_count == 3


# --- Separate read/write bus addresses (Wolf modules with divergent buses) ---


@pytest.mark.asyncio
async def test_write_uses_write_address_not_read(
    hass: HomeAssistant, catalog,
) -> None:
    """A write must be sent to the device's WRITE bus address.

    Wolf modules (BM-2, CHA) listen for write commands on a different bus
    address than the one they report reads on. The coordinator stamps the
    InfoWrite with the write address; sending it to the read address is the
    bug that made the ISM7 drop the connection on every write.
    """
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    write_resp = TelegramBundleResp(
        bundle_id="3", state="ok",
        write_telegrams=[InfonumberWriteResp(
            info_number=0x010C, bus_address="0;0;0;9",
            state="ok", db_low="14", db_high="00",
        )],
    )
    client = make_client_mock()
    # Init = pull + subscribe (both ``request``); the write is the 3rd.
    client.request = AsyncMock(side_effect=[
        TelegramBundleResp(bundle_id="1", state="ok"),  # initial pull
        TelegramBundleResp(bundle_id="2", state="ok"),  # subscribe ack
        write_resp,
    ])
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_split_rw(),
        selected=[(1, 0x010C)],
    )
    await coord.async_setup()

    await coord.async_write(device_id=1, property_id=0x010C, value=20)
    write = client.request.await_args_list[2].args[0]
    assert isinstance(write, TelegramBundleReq)
    assert write.bundle_type == "write"
    # The InfoWrite carries the WRITE address, not the read address.
    assert write.info_writes[0].bus_address == "0;0;0;9"
    assert coord.data[(1, 0x010C)] == 20


@pytest.mark.asyncio
async def test_reads_use_read_address_when_write_differs(
    hass: HomeAssistant, catalog,
) -> None:
    """Initial pull + subscribe must use the READ address, never the write.

    With a device whose write address diverges from its read address, every
    InfoRead the coordinator emits (pull snapshot and push subscribe) must
    carry the read address -- the gateway answers reads on that bus only.
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_split_rw(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()

    # Init issues pull then subscribe, both via ``request`` -- both must
    # carry the READ address.
    pull = client.request.await_args_list[0].args[0]
    assert isinstance(pull, TelegramBundleReq)
    assert pull.bundle_type == "pull"
    assert pull.info_reads[0].bus_address == "0;0;0;1"  # read address
    push = client.request.await_args_list[1].args[0]
    assert isinstance(push, TelegramBundleReq)
    assert push.bundle_type == "push"
    assert push.info_reads[0].bus_address == "0;0;0;1"  # read address


@pytest.mark.asyncio
async def test_push_decoded_by_read_address_with_split_rw(
    hass: HomeAssistant, catalog,
) -> None:
    """A push frame on the READ address decodes for a split-rw device.

    The telegram comes back on the read address; ``_process_telegrams``
    must match it to the device by the read address (not the write
    address) and land the decoded value in ``data``.
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_split_rw(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()

    callback = client.on_update.call_args.args[0]
    callback(TelegramBundleResp(
        bundle_id="push_initial", state="ok",
        read_telegrams=[
            InfonumberReadResp(
                info_number=0x0080, bus_address="0;0;0;1",  # read address
                state="ok", db_low="F5", db_high="00",
            ),
        ],
    ))
    await asyncio.sleep(0)
    assert coord.data[(1, 0x0080)] == 24.5


@pytest.mark.asyncio
async def test_write_falls_back_to_read_address_when_write_empty(
    hass: HomeAssistant, catalog, caplog,
) -> None:
    """A legacy 2-element entry -> write falls back to the read address.

    ``_normalise_device_map`` already defaults a 2-element entry's write
    address to its read address, so the write is sent to the read address
    with no warning. This test pins that behaviour via the public
    constructor (legacy ``device_map`` shape) -- the write must still go
    through, addressed to the read bus.
    """
    catalog.devices["CHA-10"].parameters[0x010C] = ParameterDefinition(
        property_id=0x010C, name="sollwert", display_name="Sollwert",
        codec=Int16Codec(min_value=5, max_value=30, unit="°C"),
    )
    write_resp = TelegramBundleResp(
        bundle_id="3", state="ok",
        write_telegrams=[InfonumberWriteResp(
            info_number=0x010C, bus_address="0;0;0;1",
            state="ok", db_low="14", db_high="00",
        )],
    )
    client = make_client_mock()
    # Init = pull + subscribe (both ``request``); the write is the 3rd.
    client.request = AsyncMock(side_effect=[
        TelegramBundleResp(bundle_id="1", state="ok"),  # initial pull
        TelegramBundleResp(bundle_id="2", state="ok"),  # subscribe ack
        write_resp,
    ])
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_with_bus(),  # legacy 2-tuple
        selected=[(1, 0x010C)],
    )
    await coord.async_setup()

    await coord.async_write(device_id=1, property_id=0x010C, value=20)
    write = client.request.await_args_list[2].args[0]
    assert write.info_writes[0].bus_address == "0;0;0;1"


@pytest.mark.asyncio
async def test_write_address_for_accessors(
    hass: HomeAssistant, catalog,
) -> None:
    """``read_address_for`` / ``write_address_for`` expose the split addresses.

    ``bus_address_for`` is kept as a back-compat alias of
    ``read_address_for``.
    """
    client = make_client_mock()
    coord = EcothermWolfCoordinator(
        hass=hass, client=client, catalog=catalog,
        device_map=_device_map_split_rw(),
        selected=[(1, 0x0080)],
    )
    await coord.async_setup()

    assert coord.read_address_for(1) == "0;0;0;1"
    assert coord.write_address_for(1) == "0;0;0;9"
    # Back-compat alias still returns the read address.
    assert coord.bus_address_for(1) == "0;0;0;1"
    # device_map property exposes the full 3-tuple.
    assert coord.device_map[1] == ("CHA-10", "0;0;0;1", "0;0;0;9")
