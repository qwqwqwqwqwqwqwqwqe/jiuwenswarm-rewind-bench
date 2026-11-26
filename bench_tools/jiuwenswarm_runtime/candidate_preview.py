"""Display historical text without treating it as an executable filesystem path."""
def denormalize_previews(value,manager):
    if isinstance(value,str):
        if 'replay://workspace/' not in value:return value
        try:return manager.denormalize_paths(value)
        except Exception as error:
            from replay.errors import WorkspaceSafetyError
            if isinstance(error,(ValueError,WorkspaceSafetyError)):return value
            raise
    if isinstance(value,list):return [denormalize_previews(v,manager) for v in value]
    if isinstance(value,tuple):return tuple(denormalize_previews(v,manager) for v in value)
    if isinstance(value,dict):return {k:denormalize_previews(v,manager) for k,v in value.items()}
    return value
