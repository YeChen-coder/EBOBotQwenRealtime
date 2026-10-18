"""Robot speaker exposed as a standard Home Assistant media player."""

from __future__ import annotations

from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EboEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one speaker entity for this robot."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EboSpeaker(coordinator, entry)])


class EboSpeaker(EboEntity, MediaPlayerEntity):
    """Play an audio URL through the EBO talkback channel.

    The add-on accepts any source FFmpeg can read and performs the EBO-specific
    conversion. Keeping that detail behind MediaPlayerEntity lets TTS and AI
    automations target the robot without knowing that it is an EBO.
    """

    _attr_name = "Speaker"
    _attr_icon = "mdi:robot-happy-outline"
    _attr_state = MediaPlayerState.IDLE
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.VOLUME_SET
    )

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "speaker")
        self._attr_media_content_id: str | None = None
        self._attr_media_content_type: MediaType | str | None = None

    @property
    def volume_level(self) -> float | None:
        """Return the EBO talkback volume on Home Assistant's 0..1 scale."""
        try:
            return max(0.0, min(1.0, float(self._state.get("talkback_volume")) / 100.0))
        except (TypeError, ValueError):
            return None

    async def async_set_volume_level(self, volume: float) -> None:
        """Set how loudly talkback audio plays through the robot."""
        await self.coordinator.cmd(
            self._node, "talkback_volume/set", round(max(0.0, min(1.0, volume)) * 100)
        )

    async def async_play_media(
        self,
        media_type: MediaType | str,
        media_id: str,
        **kwargs: Any,
    ) -> None:
        """Resolve a Home Assistant media source and send it to the EBO speaker."""
        if media_source.is_media_source_id(media_id):
            resolved = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = resolved.url
        source = async_process_play_media_url(self.hass, media_id)
        self._attr_media_content_id = media_id
        self._attr_media_content_type = media_type
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()
        try:
            # The bridge queues this source while a sleeping robot reconnects,
            # then starts it only when the new RTC audio sender is ready.
            await self.coordinator.cmd(self._node, "talk", source)
        finally:
            # The add-on accepts playback asynchronously and does not report a
            # reliable end timestamp. Return to idle instead of leaving a stale
            # "playing" state indefinitely.
            self._attr_state = MediaPlayerState.IDLE
            self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Stop the active or queued talkback audio."""
        await self.coordinator.cmd(self._node, "talk/stop", "")
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()
