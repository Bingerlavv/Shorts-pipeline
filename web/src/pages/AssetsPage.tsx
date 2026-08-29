import { useState } from "react";
import { api } from "../api";
import { Banner, Empty, formatDate } from "../components/ui";
import { useAsync } from "../hooks/useLiveState";
import type { Asset } from "../types";

const KINDS: { key: string; label: string; hint: string; accept: string }[] = [
  {
    key: "mask",
    label: "Маска",
    hint: "PNG с альфа-каналом или видео-оверлей. Растягивается на весь кадр 1080×1920.",
    accept: ".png,.webp,.mov,.webm,.mp4",
  },
  {
    key: "banner",
    label: "Баннер",
    hint: "Накладывается поверх кадра. Зелёный фон вырезается хромакеем.",
    accept: ".png,.webp,.mov,.webm,.mp4,.gif",
  },
  {
    key: "background",
    label: "Фон",
    hint:
      "Вертикальные ролики для формата «видео на фоне». Загрузи сколько нужно — " +
      "на каждый фрагмент берётся один, тот же при пересборке. " +
      "Включается в пресете: Монтаж → Фон под видео.",
    accept: ".mp4,.mov,.webm,.mkv",
  },
  {
    key: "lut",
    label: "LUT",
    hint: "Цветовая таблица .cube или .3dl для фильтра.",
    accept: ".cube,.3dl",
  },
  {
    key: "font",
    label: "Шрифт",
    hint: "TTF или OTF для заголовков и субтитров.",
    accept: ".ttf,.otf",
  },
];

export default function AssetsPage() {
  const assets = useAsync<Asset[]>(() => api.assets.list(), []);
  const [kind, setKind] = useState("mask");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const upload = async (file: File) => {
    setBusy(true);
    setError("");
    try {
      await api.assets.upload(kind, file, file.name);
      assets.reload();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const current = KINDS.find((item) => item.key === kind)!;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Ассеты</h1>
          <p>
            Файлы, на которые ссылаются настройки монтажа. После загрузки подставь ID
            ассета в соответствующее поле пресета.
          </p>
        </div>
      </div>

      {error && <Banner tone="err">{error}</Banner>}

      <div className="card" style={{ marginBottom: 18 }}>
        <div className="row" style={{ marginBottom: 10 }}>
          {KINDS.map((item) => (
            <button
              key={item.key}
              className={`small${kind === item.key ? " primary" : ""}`}
              onClick={() => setKind(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 0 }}>
          {current.hint}
        </p>
        <input
          type="file"
          accept={current.accept}
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) upload(file);
            event.target.value = "";
          }}
        />
      </div>

      {(assets.data ?? []).length === 0 ? (
        <Empty>Ассетов пока нет.</Empty>
      ) : (
        <div className="card" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th style={{ width: 60 }}>ID</th>
                <th>Название</th>
                <th>Тип</th>
                <th className="nowrap">Размер</th>
                <th className="nowrap">Загружен</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(assets.data ?? []).map((asset) => (
                <tr key={asset.id}>
                  <td className="mono">{asset.id}</td>
                  <td>
                    <a href={api.assets.fileUrl(asset.id)} target="_blank" rel="noreferrer">
                      {asset.name}
                    </a>
                  </td>
                  <td>{KINDS.find((item) => item.key === asset.kind)?.label ?? asset.kind}</td>
                  <td className="mono nowrap">{(asset.size / 1024 / 1024).toFixed(2)} МБ</td>
                  <td className="muted nowrap">{formatDate(asset.created_at)}</td>
                  <td style={{ textAlign: "right" }}>
                    <button
                      className="small danger"
                      onClick={async () => {
                        if (!confirm(`Удалить «${asset.name}»?`)) return;
                        await api.assets.remove(asset.id);
                        assets.reload();
                      }}
                    >
                      Удалить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
