from __future__ import annotations

import asyncio

import pytest

from custom_components.ecotherm_wolf.ism7.protocol import (
    DirectLogonReq,
    DirectLogonResp,
    decode_frame_header,
    decode_payload,
    encode_frame,
)
from tests.ism7.mock_server import MockIsm7Server


@pytest.mark.asyncio
async def test_mock_accepts_login_and_returns_session():
    async with MockIsm7Server(password="ism7") as srv:
        reader, writer = await asyncio.open_connection(srv.host, srv.port)
        writer.write(encode_frame(DirectLogonReq(password="ism7")))
        await writer.drain()
        header = await reader.readexactly(6)
        length, ptype = decode_frame_header(header)
        payload = await reader.readexactly(length)
        resp = decode_payload(ptype, payload)
        assert isinstance(resp, DirectLogonResp)
        assert resp.state == "ok"
        assert resp.session_id == "mock-1"
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_mock_rejects_bad_password():
    async with MockIsm7Server(password="ism7") as srv:
        reader, writer = await asyncio.open_connection(srv.host, srv.port)
        writer.write(encode_frame(DirectLogonReq(password="wrong")))
        await writer.drain()
        header = await reader.readexactly(6)
        length, ptype = decode_frame_header(header)
        payload = await reader.readexactly(length)
        resp = decode_payload(ptype, payload)
        assert isinstance(resp, DirectLogonResp)
        assert resp.state == "error"
        writer.close()
        await writer.wait_closed()
