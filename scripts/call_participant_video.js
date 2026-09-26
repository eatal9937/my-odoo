/* @odoo-module */

import {
    Component,
    onMounted,
    onPatched,
    onWillUnmount,
    status,
    useExternalListener,
    useRef,
    useState,
} from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * @typedef {Object} Props
 * @property {import("models").RtcSession} session
 * @extends {Component<Props, Env>}
 */
export class CallParticipantVideo extends Component {
    static props = ["session", "type", "inset?"];
    static template = "discuss.CallParticipantVideo";

    setup() {
        this.rtc = useState(useService("discuss.rtc"));
        this.root = useRef("root");
        this.state = useState({
            debugInfo: "",
            isPaused: true,
            hasTrack: false,
        });

        onMounted(() => {
            this._update();
            this._startMonitor();
        });
        onPatched(() => this._update());
        onWillUnmount(() => {
            if (this._monitorInterval) {
                clearInterval(this._monitorInterval);
            }
        });

        useExternalListener(this.env.bus, "RTC-SERVICE:PLAY_MEDIA", async () => {
            await this.play();
        });
    }

    _startMonitor() {
        if (this._monitorInterval) {
            clearInterval(this._monitorInterval);
        }
        this._monitorInterval = setInterval(async () => {
            const el = this.root.el;
            const stream = this.props.session ? this.props.session.getStream(this.props.type) : undefined;
            if (!el || !stream) {
                this.state.debugInfo = "No stream";
                this.state.isPaused = true;
                return;
            }
            const tracks = stream.getVideoTracks();
            this.state.hasTrack = tracks.length > 0;
            const trackInfo = tracks.map((t) => `${t.readyState}${t.muted ? "(muted)" : "(active)"}`).join(", ") || "no tracks";

            let codec = "";
            const pc = this.props.session?.peerConnection;
            if (pc && pc.getStats) {
                try {
                    const stats = await pc.getStats();
                    for (const r of stats.values()) {
                        if ((r.type === "inbound-rtp" || r.type === "outbound-rtp") && r.kind === "video") {
                            if (r.codecId) {
                                const c = stats.get(r.codecId);
                                if (c && c.mimeType) {
                                    codec = " " + c.mimeType.replace("video/", "");
                                    break;
                                }
                            }
                        }
                    }
                } catch (_e) {}
            }

            const videoInfo = `${el.videoWidth}x${el.videoHeight} ${el.paused ? "PAUSED" : "PLAY"}${codec}`;
            this.state.debugInfo = `${trackInfo} | ${videoInfo}`;
            this.state.isPaused = el.paused || el.videoWidth === 0;
            if (el.paused && tracks.some((t) => !t.muted)) {
                this.play().catch(() => {});
            }
        }, 1500);
    }

    _update() {
        const el = this.root.el;
        if (!el) {
            return;
        }
        const stream = this.props.session ? this.props.session.getStream(this.props.type) : undefined;
        if (!stream) {
            el.srcObject = null;
            return;
        }
        // Configure iOS / WKWebView attributes BEFORE srcObject
        el.muted = true;
        el.defaultMuted = true;
        el.volume = 0;
        el.playsInline = true;
        el.setAttribute("playsinline", "");
        el.setAttribute("webkit-playsinline", "");
        el.setAttribute("autoplay", "");
        el.setAttribute("muted", "");

        if (el.srcObject !== stream) {
            el.srcObject = stream;
            for (const track of stream.getVideoTracks()) {
                track.addEventListener("unmute", () => {
                    this.play().catch(() => {});
                });
            }
            stream.addEventListener("addtrack", (ev) => {
                ev.track?.addEventListener("unmute", () => {
                    this.play().catch(() => {});
                });
                this.play().catch(() => {});
            });
        }
        this.play();
    }

    async play() {
        const el = this.root.el;
        if (!el) {
            return;
        }
        try {
            el.muted = true;
            el.defaultMuted = true;
            el.volume = 0;
            el.playsInline = true;
            await el.play?.();
            this.state.isPaused = el.paused;
            if (this.props.session) {
                this.props.session.videoError = undefined;
            }
        } catch (error) {
            if (status(this) === "destroyed") {
                return;
            }
            this.state.isPaused = true;
            if (this.props.session) {
                this.props.session.videoError = error.name;
            }
        }
    }

    async forcePlay(ev) {
        ev?.stopPropagation?.();
        const el = this.root.el;
        if (el) {
            el.muted = true;
            el.defaultMuted = true;
            el.volume = 0;
            el.playsInline = true;
            const stream = this.props.session ? this.props.session.getStream(this.props.type) : undefined;
            if (stream) {
                el.srcObject = stream;
            }
            await el.play?.().catch(() => {});
            this.state.isPaused = el.paused;
        }
        await this.play();
    }

    async onVideoLoadedMetaData() {
        await this.play();
    }
}
