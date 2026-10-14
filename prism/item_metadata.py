"""Shared asset metadata, independent of a graphics object."""

class ItemMetadataMixin:
    @property
    def categories(self):
        return self._categories

    @categories.setter
    def categories(self, value):
        self._categories = list(value) if value else []
        self._notify_metadata_changed()

    @property
    def tags(self):
        return self._tags

    @tags.setter
    def tags(self, value):
        self._tags = list(value) if value else []
        self._notify_metadata_changed()

    @property
    def rating(self):
        return self._rating

    @rating.setter
    def rating(self, value):
        self._rating = max(0, min(5, int(value or 0)))
        self._notify_metadata_changed()

    @property
    def notes(self):
        return self._notes

    @notes.setter
    def notes(self, value):
        self._notes = value or ''
        self._notify_metadata_changed()

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value or ''
        self._notify_metadata_changed()

    @property
    def canvas_id(self):
        """素材属于哪个画布页 —— 拖到左栏另一个画布时改的就是它。

        `ChangeMetadata` 靠这个名字读旧值，所以它得是个真属性，
        不能只留一个 `_canvas_id`。
        """
        return self._canvas_id

    @canvas_id.setter
    def canvas_id(self, value):
        self._canvas_id = value
        self._notify_metadata_changed()

    def init_metadata(self):
        """Initialise metadata fields with empty defaults."""
        self._group_id = None
        self._group_note = ''
        if not hasattr(self, '_categories'):
            self._categories = []
        if not hasattr(self, '_tags'):
            self._tags = []
        if not hasattr(self, '_rating'):
            self._rating = 0
        if not hasattr(self, '_notes'):
            self._notes = ''
        if not hasattr(self, '_title'):
            self._title = ''
        if not hasattr(self, '_canvas_id'):
            self._canvas_id = 'default-canvas'
        if not hasattr(self, '_deleted_at'):
            self._deleted_at = None

    def _notify_metadata_changed(self):
        if self.scene() and hasattr(self.scene(), 'metadata_changed'):
            self.scene().metadata_changed.emit()

    def get_metadata_save_data(self):
        data = {'categories': list(self._categories),
                'lastUsed': getattr(self, '_last_used', ''),
                'tags': list(self._tags),
                'rating': self._rating,
                'notes': self._notes,
                'title': self._title,
                'canvasId': self._canvas_id,
                'groupId': self._group_id,
                'groupNote': self._group_note}
        # 软删除标记只有素材被丢进回收站时才存在；正常素材不写这个键，
        # 老文件读进来也不受影响。
        deleted_at = getattr(self, '_deleted_at', None)
        if deleted_at is not None:
            data['deletedAt'] = deleted_at
        return data

    def load_metadata_from_data(self, data):
        self._last_used = data.get('lastUsed', '')
        self._group_id = data.get('groupId')
        self._group_note = data.get('groupNote', '')
        self._categories = list(data.get('categories', []))
        self._tags = list(data.get('tags', []))
        self._rating = max(0, min(5, int(data.get('rating', 0) or 0)))
        self._notes = data.get('notes', '')
        self._title = data.get('title', '')
        self._canvas_id = data.get('canvasId', 'default-canvas')
        self._deleted_at = data.get('deletedAt')
