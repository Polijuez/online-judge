import os
import re

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.urls import reverse

if os.altsep:
    def split_path_first(path, repath=re.compile('[%s]' % re.escape(os.sep + os.altsep))):
        return repath.split(path, 1)
else:
    def split_path_first(path):
        return path.split(os.sep, 1)


class ProblemFileStorage(FileSystemStorage):
    def __init__(self, root=None):
        if root is None:
            root = settings.DMOJ_PROBLEM_DATA_ROOT
        super(ProblemFileStorage, self).__init__(root)
        self.url_solve = 'problem_data_file'

    def url(self, name):
        path = split_path_first(name)
        if len(path) != 2:
            raise ValueError('This file is not accessible via a URL.')
        return reverse(self.url_solve, args=path)

    def _save(self, name, content):
        if self.exists(name):
            self.delete(name)
        return super(ProblemFileStorage, self)._save(name, content)

    def get_available_name(self, name, max_length=None):
        return name

    def rename(self, old, new):
        return os.rename(self.path(old), self.path(new))


class ProblemDataStorage(ProblemFileStorage):
    pass


class ProblemMediaStorage(ProblemFileStorage):
    def __init__(self):
        super().__init__(settings.DMOJ_PROBLEM_MEDIA_ROOT)
        self.url_solve = 'problem_media_file'
