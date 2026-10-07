"""Common media choices and filtering for UI and deployment."""
MEDIA_TYPES = {
    'video': ('视频 (.mp4/.mov)', {'.mp4','.mov','.mkv','.flv'}),
    'audio': ('音频 (.mp3/.m4a)', {'.mp3','.m4a','.wav','.aac'}),
    'image': ('图片 (.png/.jpg)', {'.png','.jpg','.jpeg','.webp'}),
}


def selected_extensions(account):
    return set().union(*(MEDIA_TYPES[key][1] for key in account.media_types if key in MEDIA_TYPES))


def media_type(path):
    return next((key for key, (_, extensions) in MEDIA_TYPES.items() if path.suffix.lower() in extensions), None)
