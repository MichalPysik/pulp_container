from gettext import gettext as _
import logging

from pulpcore.plugin.models import Artifact, ProgressBar, Repository, RepositoryVersion  # noqa
from pulpcore.plugin.stages import ArtifactDownloader, DeclarativeVersion

from .sync_stages import InterrelateContent, ProcessContentStage, TagListStage
from pulp_docker.app.models import DockerRemote
from pulp_docker.app.tasks.stages.dedupe_save import SerialArtifactSave, SerialContentSave


log = logging.getLogger(__name__)


def synchronize(remote_pk, repository_pk):
    """
    Sync content from the remote repository.

    Create a new version of the repository that is synchronized with the remote.

    Args:
        remote_pk (str): The remote PK.
        repository_pk (str): The repository PK.

    Raises:
        ValueError: If the remote does not specify a URL to sync

    """
    remote = DockerRemote.objects.get(pk=remote_pk)
    repository = Repository.objects.get(pk=repository_pk)

    if not remote.url:
        raise ValueError(_('A remote must have a url specified to synchronize.'))

    DockerDeclarativeVersion(repository, remote).create()


class DockerDeclarativeVersion(DeclarativeVersion):
    """
    Subclassed Declarative version creates a custom pipeline for Docker sync.
    """

    def __init__(self, repository, remote, mirror=True):
        self.repository = repository
        self.remote = remote
        self.mirror = mirror

    def pipeline_stages(self, new_version):
        """
        Build the list of pipeline stages feeding into the
        ContentUnitAssociation stage.

        Args:
            new_version (:class:`~pulpcore.plugin.models.RepositoryVersion`): The
                new repository version that is going to be built.

        Returns:
            list: List of :class:`~pulpcore.plugin.stages.Stage` instances
        """
        # We only want to create a single instance of each stage. Each call to the stage is
        # encapsulated, so it isn't necessary to create a new instance. Also, stages that run
        # concurrent calls (the ArtifactDownloader) need to be in a single instance to ensure that
        # max_concurrent is respected together, not individually.
        downloader = ArtifactDownloader()
        serial_artifact_save = SerialArtifactSave()
        serial_content_save = SerialContentSave()
        process_content = ProcessContentStage(self.remote)
        return [
            TagListStage(self.remote),

            # Group handles Tags, Manifest Lists, and Manifests
            downloader,
            serial_artifact_save,
            process_content,
            serial_content_save,

            # Group handles Manifests and ManifestBlobs
            downloader,
            serial_artifact_save,
            process_content,
            serial_content_save,

            # Group handles ManifestBlobs only
            downloader,
            serial_artifact_save,
            process_content,
            serial_content_save,

            # Requires that all content (and related content) is already saved. By the time a
            # ManifestBlob gets here, the Manifest that contains it has already been saved. By the
            # time a Manifest gets here, the ManifestList has already been saved.
            InterrelateContent(),

            # TODO custom add/remove stages with enforced uniqueness
        ]
