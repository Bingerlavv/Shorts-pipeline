import { useState } from "react";
import { api } from "../api";
import type { Project } from "../types";
import { Banner, Modal } from "./ui";

/**
 * Удаление проекта — решение из двух частей: убрать запись из панели и,
 * отдельно, стереть скачанный исходник с готовыми роликами. Второе занимает
 * гигабайты и необратимо, поэтому спрашиваем прямо, а не прячем в confirm().
 */
export function DeleteProjectDialog({
  project,
  onClose,
  onDeleted,
}: {
  project: Pick<Project, "id" | "title" | "source_url" | "segment_count">;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const remove = async () => {
    setBusy(true);
    setError("");
    try {
      await api.projects.remove(project.id, deleteFiles);
      onDeleted();
    } catch (exc) {
      setError((exc as Error).message);
      setBusy(false);
    }
  };

  return (
    <Modal title="Удалить проект" onClose={onClose}>
      <p style={{ marginTop: 0 }}>
        <b>{project.title || project.source_url}</b>
      </p>
      <p className="muted" style={{ fontSize: 13 }}>
        Из панели исчезнут сам проект, его расшифровка и{" "}
        {project.segment_count > 0
          ? `${project.segment_count} найденных фрагментов`
          : "все найденные фрагменты"}
        . Уже опубликованные ролики останутся на YouTube и в Instagram — панель их не
        трогает.
      </p>

      <label className="check" style={{ marginTop: 16 }}>
        <input
          type="checkbox"
          checked={deleteFiles}
          onChange={(event) => setDeleteFiles(event.target.checked)}
        />
        Стереть с диска исходник и смонтированные ролики
      </label>
      <p className="config-hint">
        {deleteFiles
          ? "Файлы будут удалены безвозвратно. Скачивать и монтировать придётся заново."
          : "Файлы останутся в папке storage — место на диске не освободится."}
      </p>

      {error && <Banner tone="err">{error}</Banner>}

      <div className="row" style={{ marginTop: 18 }}>
        <button className="danger" disabled={busy} onClick={remove}>
          {busy ? "Удаляю…" : deleteFiles ? "Удалить проект и файлы" : "Удалить проект"}
        </button>
        <button className="ghost" onClick={onClose} disabled={busy}>
          Отмена
        </button>
      </div>
    </Modal>
  );
}
