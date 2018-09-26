from urllib.parse import urljoin
import json
import logging

from pulpcore.plugin.models import Artifact, ProgressBar
from pulpcore.plugin.stages import DeclarativeArtifact, DeclarativeContent, Stage

from pulp_docker.app.models import (ImageManifest, MEDIA_TYPE, ManifestBlob,
                                    ManifestList, Tag, BlobManifestBlob, ManifestListManifest)


log = logging.getLogger(__name__)


V2_ACCEPT_HEADERS = {
    'accept': ','.join([MEDIA_TYPE.MANIFEST_V2, MEDIA_TYPE.MANIFEST_LIST])
}


class TagListStage(Stage):
    """
    The first stage of a pulp_docker sync pipeline.
    """
    def __init__(self, remote):
        self.remote = remote

    async def __call__(self, in_q, out_q):
        """
        Build and emit `DeclarativeContent` for each Tag.

        Args:
            in_q (asyncio.Queue): Unused because the first stage doesn't read from an input queue.
            out_q (asyncio.Queue): Tag `DeclarativeContent` objects are sent here.

        """
        with ProgressBar(message="Downloading Tags List") as pb:
            log.debug("Fetching tags list for upstream repository: {repo}".format(
                repo=self.remote.upstream_name
            ))
            relative_url = '/v2/{name}/tags/list'.format(name=self.remote.namespaced_upstream_name)
            tag_list_url = urljoin(self.remote.url, relative_url)
            list_downloader = self.remote.get_downloader(tag_list_url)
            await list_downloader.run()

        with open(list_downloader.path) as tags_raw:
            tags_dict = json.loads(tags_raw.read())
            tag_list = tags_dict['tags']
        pb.increment()
        # TODO add pagination
        log.warn("Tag list len {num}".format(num=len(tag_list)))
        for tag_name in tag_list:
            tag_dc = self.create_pending_tag(tag_name)
            await out_q.put(tag_dc)
            pb.increment()

        await out_q.put(None)

    def create_pending_tag(self, tag_name):
        """
        Create `DeclarativeContent` for each tag.

        Each dc contains enough information to be dowloaded by an ArtifactDownload Stage.

        Args:
            tag_name (str): Name of each tag

        Returns:
            pulpcore.plugin.stages.DeclarativeContent: A Tag DeclarativeContent object
        """
        relative_url = '/v2/{name}/manifests/{tag}'.format(
            name=self.remote.namespaced_upstream_name,
            tag=tag_name,
        )
        url = urljoin(self.remote.url, relative_url)
        tag = Tag(name=tag_name)
        # TODO this one, we don't have anything to add here, so we can't do validation
        manifest_artifact = Artifact()
        da = DeclarativeArtifact(
            artifact=manifest_artifact,
            url=url,
            relative_path="TODO-where-should-this-go-{name}".format(name=tag_name),
            remote=self.remote,
            # TODO is this necessary for tag list?
            extra_data={'headers': V2_ACCEPT_HEADERS}
        )
        tag_dc = DeclarativeContent(content=tag, d_artifacts=[da])
        return tag_dc


class ProcessContentStage(Stage):
    """
    Process all Manifests, Manifest Lists, and Tags.

    For each processed type, create content from nested fields. This stage does not process
    ManifestBlobs, which do not contain nested content.
    """
    def __init__(self, remote):
        self.remote = remote
        # TODO remove debugging tool

    async def __call__(self, in_q, out_q):
        _skipped_schema_1_count = 0
        _tagged_manifest_list_count = 0
        _tagged_manifest_count = 0
        _manifest_list_count = 0
        _manifest_count = 0
        while True:
            dc = await in_q.get()
            if dc is None:
                break
            # If content has been saved, it has also been processed.
            elif dc.content.pk is not None:
                await out_q.put(dc)
                continue
            # We don't need to process blobs.
            elif type(dc.content) is ManifestBlob:
                await out_q.put(dc)
                continue

            # TODO All docker content contains a single artifact.
            assert len(dc.d_artifacts) == 1
            with dc.d_artifacts[0].artifact.file.open() as content_file:
                raw = content_file.read()
            content_data = json.loads(raw)

            if type(dc.content) is Tag:
                if content_data.get('mediaType') == MEDIA_TYPE.MANIFEST_LIST:
                    await self.create_and_process_tagged_manifest_list(dc, content_data, out_q)
                    await out_q.put(dc)
                    _tagged_manifest_list_count += 1
                elif content_data.get('mediaType') == MEDIA_TYPE.MANIFEST_V2:
                    await self.create_and_process_tagged_manifest(dc, content_data, out_q)
                    await out_q.put(dc)
                    _tagged_manifest_count += 1
                else:
                    assert content_data.get('schemaVersion') == 1
                    _skipped_schema_1_count += 1
            elif type(dc.content) is ImageManifest:
                # TODO add config layer
                _manifest_count += 1
                for layer in content_data.get("layers"):
                    await self.create_pending_blob(dc, layer, out_q)
                await out_q.put(dc)
            elif type(dc.content) is ManifestList:
                _manifest_list_count += 1
                # TODO remove or make custom exception
                raise Exception("Manifest Lists should have been downloaded and saved as tags.")
            else:
                # TODO remove or make custom exception
                msg = "Unexpected type cannot be processed{tp}".format(tp=type(dc.content))
                raise Exception(msg)

        # TODO remove debugging numbers
        # is it possible to track these with a multiple progress bars?
        # This is here because I think we are leaking content somewhere, maybe dedupe save?
        log.info("Skippped {n}".format(n=_skipped_schema_1_count))
        log.info("tagged MLs: {n}".format(n=_tagged_manifest_list_count))
        log.info("tagged Ms: {n}".format(n=_tagged_manifest_count))
        log.info("Ms: {n}".format(n=_manifest_count))
        log.info("MLs: {n}".format(n=_manifest_list_count))
        # TODO how to track blobs?
        await out_q.put(None)

    async def create_and_process_tagged_manifest_list(self, tag_dc, manifest_list_data, out_q):
        # TODO(test this) dc that comes here should always be a tag
        manifest_list = ManifestList(
            digest="sha256:{digest}".format(digest=tag_dc.d_artifacts[0].artifact.sha256),
            schema_version=manifest_list_data['schemaVersion'],
            media_type=manifest_list_data['mediaType'],
        )
        list_dc = DeclarativeContent(content=manifest_list, d_artifacts=[tag_dc.d_artifacts[0]])
        for manifest in manifest_list_data.get('manifests'):
            await self.create_pending_manifest(list_dc, manifest, out_q)
        list_dc.extra_data['relation'] = tag_dc

        await out_q.put(list_dc)

    async def create_and_process_tagged_manifest(self, tag_dc, manifest_data, out_q):
        # tagged manifests actually have an artifact already that we need to use.
        manifest = ImageManifest(
            digest=tag_dc.d_artifacts[0].artifact.sha256,
            schema_version=manifest_data['schemaVersion'],
            media_type=manifest_data['mediaType'],
        )
        # extra_data="TODO(asmacdo) add reference to tag"
        man_dc = DeclarativeContent(content=manifest, d_artifacts=[tag_dc.d_artifacts[0]])
        # TODO add config layer
        for layer in manifest_data.get('layers'):
            await self.create_pending_blob(man_dc, layer, out_q)
        man_dc.extra_data['relation'] = tag_dc
        await out_q.put(man_dc)

    async def create_pending_manifest(self, list_dc, manifest_data, out_q):
        digest = manifest_data['digest']
        relative_url = '/v2/{name}/manifests/{digest}'.format(
            name=self.remote.namespaced_upstream_name,
            digest=digest
        )
        manifest_url = urljoin(self.remote.url, relative_url)
        # TODO since i have a digest, I should pass to the artifact here for validation.
        manifest_artifact = Artifact()
        da = DeclarativeArtifact(
            artifact=manifest_artifact,
            url=manifest_url,
            relative_path=digest,
            remote=self.remote,
            extra_data={'headers': V2_ACCEPT_HEADERS}
        )
        manifest = ImageManifest(
            digest=manifest_data['digest'],
            schema_version=2,
            media_type=manifest_data['mediaType'],
        )
        man_dc = DeclarativeContent(
            content=manifest,
            d_artifacts=[da],
            extra_data={'relation': list_dc}
        )
        await out_q.put(man_dc)

    async def create_pending_blob(self, man_dc, blob_data, out_q):
        sha256 = blob_data['digest'],
        # TODO since i have a digest, I should pass to the artifact here for validation.
        blob_artifact = Artifact()
        blob = ManifestBlob(
            digest=sha256,
            media_type=blob_data['mediaType'],
        )
        relative_url = '/v2/{name}/blobs/{digest}'.format(
            name=self.remote.namespaced_upstream_name,
            digest=blob_data['digest'],
        )
        blob_url = urljoin(self.remote.url, relative_url)
        da = DeclarativeArtifact(
            artifact=blob_artifact,
            url=blob_url,
            relative_path=blob_data['digest'],
            remote=self.remote,
            extra_data={'headers': V2_ACCEPT_HEADERS}
        )
        blob_dc = DeclarativeContent(
            content=blob,
            d_artifacts=[da],
            extra_data={'relation': man_dc}
        )
        await out_q.put(blob_dc)


class InterrelateContent(Stage):
    async def __call__(self, in_q, out_q):
        while True:
            dc = await in_q.get()
            if dc is None:
                break
            if dc.extra_data.get('relation'):
                if type(dc.content) is ManifestList:
                    self.relate_manifest_list(dc)
                elif type(dc.content) is ManifestBlob:
                    self.relate_blob(dc)
                elif type(dc.content) is ImageManifest:
                    self.relate_manifest(dc)

            await out_q.put(dc)
        await out_q.put(None)

    def relate_blob(self, dc):
        # TODO I think we can assume this works, no blobs can be synced without belonging to a
        # manifest. If the manifest has been processed, it almost certainly has been saved.
        related_dc = dc.extra_data.get('relation')
        # TODO invert this, try to save, and pass if it fails
        try:
            BlobManifestBlob.objects.get(manifest=related_dc.content, manifest_blob=dc.content)
        except BlobManifestBlob.DoesNotExist:
            thru = BlobManifestBlob(manifest=related_dc.content, manifest_blob=dc.content)
            thru.save()
        else:
            pass

    def relate_manifest(self, dc):
        # TODO I think we can assume this works, no manifests can be synced without belonging to a
        # manifest list or tag
        related_dc = dc.extra_data.get('relation')
        # TODO invert this, try to save, and retrieve if it fails.
        if type(related_dc) is Tag:
            related_dc.content.manifest = dc.content
        elif type(related_dc) is ManifestList:
            try:
                ManifestListManifest.objects.get(
                    manifest_list=related_dc.content, manifest=dc.content)
            except ManifestListManifest.DoesNotExist:
                thru = ManifestListManifest(manifest_list=related_dc.content, manifest=dc.content)
                thru.save()
            else:
                pass

    def relate_manifest_list(self, dc):
        related_dc = dc.extra_data.get('relation')
        assert type(related_dc.content) is Tag
        # TODO how do we want to handle uniqueness enforcement
        related_dc.content.manifest_list = dc.content


class DidItWorkStage(Stage):
    """
    TODO remove this development tool.
    """
    async def __call__(self, in_q, out_q):
        while True:
            log_it = await in_q.get()
            if log_it is None:
                break
            self.log_state(log_it)
            await out_q.put(log_it)
        await out_q.put(None)

    def log_state(self, dc):
        # TODO dont assume 1 artifact
        downloaded = dc.d_artifacts[0].artifact.file.name != ""
        dl = "D" if downloaded else "!d"
        a_saved = dc.d_artifacts[0].artifact.pk is not None
        a_s = "A" if a_saved else "!a"
        saved = dc.content.pk is not None
        sa = "S" if saved else "!s"
        log.info("{dct}: {dl}{a_s}{sa}".format(dct=type(dc.content), dl=dl, a_s=a_s, sa=sa))
