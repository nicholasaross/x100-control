"""Driver tests against the simulated projector over real TCP sockets."""

import time

import pytest

from x100 import protocol as p
from x100.mock import MockServer
from x100.pjlink import PJLink, PJLinkAuthError
from x100.projector import FunctionDisabled, NotSupported, Projector, Unreachable
from x100.protocol import PowerState
from x100.transport import TcpTransport


def wait_power(proj, target, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if proj.power() == target:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def mock():
    with MockServer(warmup=0.3, cooldown=0.2, pjlink_port=0) as srv:
        yield srv


@pytest.fixture
def proj(mock):
    pr = Projector(TcpTransport("127.0.0.1", mock.port, timeout=1.0, min_gap=0))
    yield pr
    pr.close()


def test_power_cycle(proj):
    assert proj.power() == PowerState.OFF
    proj.power_on()
    assert proj.power() == PowerState.WARMING
    assert wait_power(proj, PowerState.ON)
    proj.power_off()
    assert proj.power() == PowerState.COOLING
    assert wait_power(proj, PowerState.OFF)


def test_standby_refuses_everything_but_power(proj):
    with pytest.raises(FunctionDisabled):
        proj.select_source(0x07)
    with pytest.raises(FunctionDisabled):
        proj.volume()


def test_input_volume_mute(proj):
    proj.power_on()
    assert wait_power(proj, PowerState.ON)
    proj.select_source(0x07)
    assert proj.source() == 0x07
    proj.set_volume(17)
    assert proj.volume() == 17
    proj.volume_up()
    proj.volume_up()
    proj.volume_down()
    assert proj.volume() == 18
    proj.set_mute(True)
    assert proj.muted() is True
    proj.set_mute(False)
    assert proj.muted() is False
    assert proj.light_source_hours() == 1234


def test_invalid_source_is_refused(proj):
    proj.power_on()
    assert wait_power(proj, PowerState.ON)
    with pytest.raises(FunctionDisabled):
        proj.select_source(0x55)


def test_reconnects_when_projector_drops_session():
    with MockServer(warmup=0.1, close_after=1) as srv:
        pr = Projector(TcpTransport("127.0.0.1", srv.port, timeout=1.0, min_gap=0))
        for _ in range(4):  # every exchange needs a fresh connection
            assert pr.power() == PowerState.OFF
        pr.close()


def test_resyncs_past_line_noise():
    with MockServer(junk_prefix=b"\xff\x13\x00") as srv:
        pr = Projector(TcpTransport("127.0.0.1", srv.port, timeout=1.0, min_gap=0))
        assert pr.power() == PowerState.OFF
        pr.power_on()  # ACK after junk
        pr.close()


def test_one_way_link():
    with MockServer(warmup=0.1, one_way=True) as srv:
        two_way = Projector(TcpTransport("127.0.0.1", srv.port, timeout=0.5, min_gap=0))
        with pytest.raises(Unreachable):
            two_way.power()
        two_way.close()
        one_way = Projector(TcpTransport("127.0.0.1", srv.port, timeout=0.5, min_gap=0), one_way=True)
        one_way.power_on()  # fire and forget, doesn't wait
        with pytest.raises(NotSupported):
            one_way.power()
        time.sleep(0.3)
        with srv.state.lock:
            srv.state.tick()
            assert srv.state.power == PowerState.ON  # the command still landed
        one_way.close()


def test_unreachable_host():
    pr = Projector(TcpTransport("127.0.0.1", 1, timeout=0.5, connect_timeout=0.5))
    with pytest.raises(Unreachable):
        pr.power()


def test_pjlink(mock):
    pj = PJLink("127.0.0.1", mock.pjlink_port)
    assert pj.power() == PowerState.OFF
    pj.power_on()
    assert pj.power() == PowerState.WARMING
    info = pj.info()
    assert info["INF2"] == "X100-4K" and info["CLSS"] == "1"


def test_pjlink_password():
    with MockServer(pjlink_port=0, pjlink_password="0000") as srv:
        assert PJLink("127.0.0.1", srv.pjlink_port, password="0000").power() == PowerState.OFF
        with pytest.raises(PJLinkAuthError):
            PJLink("127.0.0.1", srv.pjlink_port).power()
        with pytest.raises(PJLinkAuthError):
            PJLink("127.0.0.1", srv.pjlink_port, password="wrong").power()


def test_raw_exchange_reports_reply(proj):
    frame = proj.exchange(p.parse_hex_bytes("07 14 00 05 00 34 00 00 11 00 5E"))
    assert frame.is_reply and frame.value == 0
