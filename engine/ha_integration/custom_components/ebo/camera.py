"""Live camera per EBO robot — RTSP source (HA's stream/go2rtc serve it as WebRTC)."""

from __future__ import annotations

from haffmpeg.tools import IMAGE_JPEG
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NODE, CONF_RTSP, CONF_SN, DOMAIN
from .entity import ebo_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the camera for one robot."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EboCamera(entry, coordinator)])


class EboCamera(Camera):
    """A robot's live camera. Entity name = the device (robot) name."""

    _attr_has_entity_name = True
    _attr_name = None  # -> the entity is named after the device (the robot)
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        super().__init__()
        data = entry.data
        self._coordinator = coordinator
        self._node = data.get(CONF_NODE)
        self._rtsp_fallback: str = data.get(CONF_RTSP) or ""
        sn = str(data.get(CONF_SN) or data.get(CONF_NODE) or entry.entry_id)
        self._attr_unique_id = f"{sn}_camera"
        # Same identifiers/MAC as the robot's other entities -> HA merges them into ONE device.
        self._attr_device_info = ebo_device_info(
            entry, (coordinator.data or {}).get(self._node))

    async def stream_source(self) -> str | None:
        """Return the RTSP URL.

        Prefer the add-on's *current* URL from the coordinator (self-heals when the add-on's
        internal hostname/IP changes), falling back to whatever was stored at add time.
        """
        robot = (self._coordinator.data or {}).get(self._node) or {}
        return robot.get(CONF_RTSP) or self._rtsp_fallback or None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Still image. Pull it from the add-on's snapshot endpoint (reliable) instead of letting
        HA extract a keyframe from the internal RTSP itself — that default path returns 500 when it
        can't grab a keyframe in time. Falls back to the default (stream) grab if the add-on can't
        provide one."""
        img = await self._coordinator.snapshot(self._node)
        if img:
            return img

        # The base Camera entity has no still-image implementation.  When the
        # engine's snapshot endpoint is unavailable, explicitly extract one
        # frame from the same RTSP source used by Home Assistant's stream
        # integration.  Keeping this fallback here also gives LLM Vision a
        # camera-agnostic JPEG instead of coupling it to an EBO-only endpoint.
        source = await self.stream_source()
        if not source:
            return None
        return await async_get_image(
            self.hass,
            source,
            output_format=IMAGE_JPEG,
            width=width,
            height=height,
        )
