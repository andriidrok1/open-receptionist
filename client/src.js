// Тонка обгортка над офіційним SDK pipecat: сторінка більше не розбирає
// протокол самотужки. Ручна реалізація коштувала чотирьох багів поспіль —
// не той тип повідомлення з текстом бота, проміжні транскрипти як репліки,
// порядок речень і обрив звуку на першій секунді.
import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

window.createVoiceClient = function createVoiceClient(baseUrl, handlers) {
  const client = new PipecatClient({
    transport: new SmallWebRTCTransport({ connectionUrl: `${baseUrl}/api/offer` }),
    enableMic: true,
    enableCam: false,
    callbacks: {
      onBotReady: () => handlers.onReady?.(),
      onUserStartedSpeaking: () => handlers.onUserStart?.(),
      onUserStoppedSpeaking: () => handlers.onUserStop?.(),
      onBotStartedSpeaking: () => handlers.onBotStart?.(),
      onBotStoppedSpeaking: () => handlers.onBotStop?.(),
      onUserTranscript: (d) => d?.final && handlers.onUserText?.(d.text),
      onBotTranscript: (d) => handlers.onBotText?.(d.text),
      onDisconnected: () => handlers.onEnd?.(),
      onError: (e) => handlers.onError?.(e),
      // Трек бота віддаємо сторінці — вона сама вирішує, куди його підключити.
      onTrackStarted: (track, participant) => {
        if (!participant?.local && track.kind === "audio") handlers.onBotAudio?.(track);
      },
    },
  });
  return client;
};
