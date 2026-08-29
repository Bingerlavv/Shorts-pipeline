import type { Account } from "../types";

const PLATFORM_LABEL: Record<string, string> = {
  youtube: "YouTube",
  instagram: "Instagram",
  tiktok: "TikTok",
};

export function PublishTargets({
  accounts,
  value,
  onChange,
}: {
  accounts: Account[];
  value: number[];
  onChange: (next: number[]) => void;
}) {
  if (accounts.length === 0) return null;

  const chosen = new Set(value);
  const toggle = (id: number) =>
    onChange(chosen.has(id) ? value.filter((item) => item !== id) : [...value, id]);

  const platforms = Object.keys(PLATFORM_LABEL).filter((platform) =>
    accounts.some((account) => account.platform === platform),
  );

  return (
    <div style={{ marginTop: 12 }}>
      <label>Куда публикуются ролики проекта</label>
      {platforms.map((platform) => (
        <div key={platform} style={{ marginTop: 6 }}>
          <span className="label">{PLATFORM_LABEL[platform]}</span>
          <div className="bg-picker">
            {accounts
              .filter((account) => account.platform === platform)
              .map((account) => (
                <label key={account.id} className="check">
                  <input
                    type="checkbox"
                    checked={chosen.has(account.id)}
                    onChange={() => toggle(account.id)}
                  />
                  <span className="truncate">
                    {account.name}
                    {account.is_active ? "" : " (отключён)"}
                  </span>
                </label>
              ))}
          </div>
        </div>
      ))}
      <div className="config-hint">
        {chosen.size === 0
          ? "Никуда. Отметь аккаунты — и все ролики проекта будут уходить в них: " +
            "и автопрогоном, и кнопкой «Опубликовать»."
          : `Отмечено ${chosen.size}. Каждый смонтированный ролик уходит в эти аккаунты. ` +
            "Ту же связь можно править со страницы «Аккаунты»."}
      </div>
    </div>
  );
}
