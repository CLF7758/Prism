"""AI inference in a worker, persisted metadata changes on the GUI thread."""
import io
import logging

from PyQt6 import QtCore, QtWidgets

from prism import commands, fileio, widgets

logger = logging.getLogger(__name__)


class TaggingWorker(fileio.ThreadedIO):
    results_ready = QtCore.pyqtSignal(object)

    def __init__(self, items, client=None, categories=(), fallback_sources=None):
        super().__init__(self._tag)
        self.items = list(items)
        self.client = client
        self.categories = list(categories)
        self.fallback_sources = fallback_sources or {}

    def _tag(self, worker=None):
        from prism.ai_inference import InferenceProcess
        results, errors = {}, []
        tagger = None
        self.begin_processing.emit(len(self.items))
        try:
            tagger = InferenceProcess(canceled=lambda: self.canceled)
            if not tagger.load_model():
                raise RuntimeError('AI 模型加载失败，请查看 Prism.log。')
            for index, item in enumerate(self.items):
                if self.canceled:
                    break
                try:
                    # The filename is historical metadata; after moving a
                    # project to another computer the archive is the source.
                    retained_in_memory = item._source_blob is not None
                    blob = item.original_bytes()
                    blob = blob or self.fallback_sources.get(item)
                    source = io.BytesIO(blob) if blob else item.filename
                    found = tagger.tag_image(source)
                    if found:
                        results[item] = {'tags': [name for name, _ in found]}
                    else:
                        errors.append(f'{item.filename or "图片"}: 未识别到标签')
                except Exception as exc:
                    logger.exception('AI tagging failed for %s', item.filename)
                    errors.append(f'{item.filename or "图片"}: {exc}')
                finally:
                    if (not retained_in_memory and
                            (item._source_path or item._prism_source)):
                        item._source_blob = None
                    self.progress.emit(index + 1)
            if self.client is not None and results and not self.canceled:
                self._postprocess(results, errors)
        except Exception as exc:
            logger.exception('AI tagging could not start')
            errors.append(str(exc))
        finally:
            if tagger is not None:
                tagger.close()
            self.fallback_sources.clear()
            # ThreadedIO ignores return values and its finished signal takes
            # (str, list), so inference needs an explicit result signal.
            self.results_ready.emit({'items': results, 'errors': errors,
                                     'canceled': self.canceled,
                                     'total': len(self.items)})
            self.finished.emit('', errors)

    def _postprocess(self, results, errors):
        client = self.client
        try:
            names = list(dict.fromkeys(
                name for result in results.values() for name in result['tags']))
            if client.is_translate_enabled():
                translated = client.translate_tags(names)
                if translated and len(translated) == len(names):
                    mapping = dict(zip(names, translated))
                    for result in results.values():
                        result['tags'] = list(dict.fromkeys(
                            result['tags'] + [mapping[name] for name in result['tags']
                                              if mapping.get(name)]))
            for result in results.values():
                if self.canceled:
                    break
                if client.is_title_enabled():
                    title = client.generate_title(result['tags'])
                    if title:
                        result['title'] = title
                if client.is_category_enabled():
                    suggestion = client.suggest_categories(
                        result['tags'], self.categories)
                    if suggestion and suggestion.get('suggested'):
                        # Keep a suggestion; do not silently move an asset.
                        result['category_suggestion'] = suggestion['suggested']
        except Exception as exc:
            logger.exception('Cloud postprocessing failed; keeping local tags')
            errors.append(f'云端后处理失败，已保留本地标签：{exc}')


def start_tagging(view, items):
    previous = getattr(view, 'worker', None)
    if previous is not None and previous.isRunning():
        widgets.PrismNotification(view, '请等待当前任务结束后再开始打标签。')
        return
    from prism.ai_client import DeepSeekClient
    client = DeepSeekClient()
    if client.is_local_only_mode() or not client.get_api_key():
        client = None
    elif not client.is_privacy_confirmed():
        answer = QtWidgets.QMessageBox.question(
            view, '隐私确认', '启用云端后处理会将标签文本发送到服务器，是否继续？',
            QtWidgets.QMessageBox.StandardButton.Yes |
            QtWidgets.QMessageBox.StandardButton.No)
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            client.set_privacy_confirmed(True)
        else:
            client = None
    fallback_sources = {}
    for item in items:
        if not item.has_original_source():
            preview = item.pixmap().scaled(
                448, 448, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
            buffer = QtCore.QBuffer()
            buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
            if preview.save(buffer, 'PNG'):
                fallback_sources[item] = bytes(buffer.data())
    view.worker = TaggingWorker(items, client, view.scene.category_names, fallback_sources)
    view.worker.results_ready.connect(view._on_ai_tag_finished)
    view.progress = widgets.PrismProgressDialog(
        f'AI 打标签 ({len(items)} 张图片)', worker=view.worker, parent=view)
    view.progress.show()
    view.worker.start()


def apply_results(view, payload):
    results = payload.get('items', {})
    changes = []
    for item, result in results.items():
        try:
            if item.scene() is view.scene:
                changes.append((item, result))
        except RuntimeError:
            # A project can have been closed while inference was running.
            continue
    if changes:
        view.undo_stack.beginMacro('AI 打标签')
        try:
            items = [item for item, _ in changes]
            values = [list(dict.fromkeys(list(item.tags) + result['tags']))
                      for item, result in changes]
            view.undo_stack.push(commands.ChangeMetadata(items, 'tags', values))
            titled = [(item, result['title']) for item, result in changes
                      if result.get('title') and not item.title]
            if titled:
                view.undo_stack.push(commands.ChangeMetadata(
                    [item for item, _ in titled], 'title',
                    [title for _, title in titled]))
            suggested = []
            for item, result in changes:
                if result.get('category_suggestion'):
                    note = f'AI 分类建议：{result["category_suggestion"]}'
                    if note not in item.notes:
                        suggested.append((item, (item.notes + '\n' + note).strip()))
            if suggested:
                view.undo_stack.push(commands.ChangeMetadata(
                    [item for item, _ in suggested], 'notes',
                    [notes for _, notes in suggested]))
        finally:
            view.undo_stack.endMacro()
    message = f'已为 {len(changes)}/{payload.get("total", len(results))} 张图片添加标签。'
    if payload.get('canceled'):
        message += '\n任务已取消，已完成的标签已保留。'
    errors = payload.get('errors', [])
    if errors:
        message += '\n' + '\n'.join(errors[:5])
        logger.warning('AI tagging errors: %s', errors)
    widgets.PrismNotification(view, message)
