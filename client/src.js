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
      // onBotOutput, а НЕ onBotTranscript: другий зібраний із потоку LLM і
      // шлеться лише по межі речення — хвіст без крапки («Консультація 300
      // грн») не приходить ніколи і випливає приклеєним до наступної репліки.
      // Сам SDK його вже позначив deprecated.
      onBotOutput: (d) => handlers.onBotText?.(d.text, d.segment_id),
      onDisconnected: () => handlers.onEnd?.(),
      onError: (e) => handlers.onError?.(e),
      // Відмова в мікрофоні НЕ ламає connect(): з'єднання підніметься зовсім
      // без аудіо-треку, сторінка буде малювати «слухаю», а бот не почує
      // нічого. Без цих двох колбеків такий дзвінок не відрізнити від
      // справного.
      onDeviceError: (e) => handlers.onMicError?.(e),
      onMediaStateChanged: (m) => handlers.onMediaState?.(m),
      // Трек бота віддаємо сторінці — вона сама вирішує, куди його підключити.
      onTrackStarted: (track, participant) => {
        if (!participant?.local && track.kind === "audio") handlers.onBotAudio?.(track);
      },
    },
  });
  return client;
};
