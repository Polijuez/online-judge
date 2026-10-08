import errno
import os

from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from judge.utils.problem_data import ProblemDataStorage,ProblemMediaStorage

__all__ = ['problem_media_storage', 'problem_data_storage', 'problem_directory_file', 'ProblemData', 'ProblemTestCase', 'ProblemMedia']

problem_media_storage = ProblemMediaStorage()
problem_data_storage = ProblemDataStorage()

def _problem_directory_file(code, filename):
    return os.path.join(code, os.path.basename(filename))


def problem_directory_file(data, filename):
    return _problem_directory_file(data.problem.code, filename)

class ProblemData(models.Model):
    problem = models.OneToOneField('Problem', verbose_name=_('problem'), related_name='data_files',
                                   on_delete=models.CASCADE)
    zipfile = models.FileField(verbose_name=_('data zip file'), storage=problem_data_storage, null=True, blank=False,
                               upload_to=problem_directory_file)
    initfile = models.FileField(verbose_name=_('init file'), storage=problem_data_storage, null=True, blank=False,
                                 upload_to=problem_directory_file)
    checker = models.FileField(verbose_name=_('custom checker'), storage=problem_data_storage, null=True, blank=True,
                                 upload_to=problem_directory_file)
    grader = models.FileField(verbose_name=_('custom grader'), storage=problem_data_storage, null=True, blank=True,
                                 upload_to=problem_directory_file)
    signature_grading = models.BooleanField(verbose_name=_('signature grading (ioi style)'), default=False)
    signature = models.FileField(verbose_name=_('signature'), storage=problem_data_storage, null=True, blank=True,
                                 upload_to=problem_directory_file)
    evaluator = models.FileField(verbose_name=_('evaluator'), storage=problem_data_storage, null=True, blank=True,
                                 upload_to=problem_directory_file)

    """ NOTE: Tener en cuenta que, cuando se crea un ProblemData, se usa una sola vez y despues se descarta. Cada vez
    que se quiere modificar o leer este modelo se vuelve a crear un objeto nuevo. Por eso llevamos el registro de la
    version original de los archivos, tal como vienen de la base de datos. Cuando se modifica esta clase podemos
    detectar que archivos son distintos de la version original y borrar el original de los que cambiaron.
    """
    __original_zipfile = None
    __original_initfile = None
    __original_checker = None
    __original_grader = None
    __original_signature = None
    __original_evaluator = None
    __files = [
        ("initfile","__original_initfile"),
        ("zipfile","__original_zipfile"),
        ("checker","__original_checker"),
        ("grader","__original_grader"),
        ("signature","__original_signature"),
        ("evaluator","__original_evaluator")
    ]

    @classmethod
    def get_file_fields(cls):
        return list(map(lambda x: x[0], cls.__files))

    @classmethod
    def get_file_originals(cls):
        return list(map(lambda x: x[1], cls.__files))
    
    def __init__(self, *args, **kwargs):
        super(ProblemData, self).__init__(*args, **kwargs)
        for file, original in ProblemData.__files:
            ## self.original = self.file
            setattr(self,original,getattr(self,file))

    def save(self, *args, **kwargs):
        ## if the user is uploading a file delete the previous one
        for file,original in ProblemData.__files:
            if getattr(self,original) and getattr(self,file) != getattr(self,original):
                ## copy and reassign because delete changes the field
                current = getattr(self,file)
                getattr(self,original).delete(save=False)
                setattr(self,file,current)
        return super(ProblemData, self).save(*args, **kwargs)

    def has_yml(self):
        if not self.initfile: return False
        return problem_data_storage.exists('%s/%s' % (self.problem.code, self.initfile.name))

    def _update_code(self, original, new):
        try:
            problem_data_storage.rename(original, new)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise
        for file,_ in ProblemData.__files:
            if getattr(self,file):
                getattr(self,file).name = _problem_directory_file(new, getattr(self,file).name)
        self.save()
    _update_code.alters_data = True

class ProblemTestCase(models.Model):
    dataset = models.ForeignKey('Problem', verbose_name=_('problem data set'), related_name='cases',
                                on_delete=models.CASCADE)
    order = models.IntegerField(verbose_name=_('case position'))
    type = models.CharField(max_length=1, verbose_name=_('case type'),
                            choices=(('C', _('Normal case')),
                                     ('S', _('Batch start')),
                                     ('E', _('Batch end'))),
                            default='C')
    input_file = models.CharField(max_length=100, verbose_name=_('input file name'), blank=True)
    output_file = models.CharField(max_length=100, verbose_name=_('output file name'), blank=True)
    points = models.IntegerField(verbose_name=_('point value'), blank=True, null=True)

class ProblemMedia(models.Model):
    dataset = models.ForeignKey('Problem', verbose_name=_('problem data set'), related_name='media',
                                on_delete=models.CASCADE)
    file = models.FileField(verbose_name=_('media file'), storage=problem_media_storage, null=True, blank=False,
                            upload_to=problem_directory_file)


