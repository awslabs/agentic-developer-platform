from pathlib import Path
import subprocess
p=Path(__file__).parent
source=Path('/workspaces/projects/security27/refresh_inventory.py').read_text().replace("root=pathlib.Path('/workspaces/projects/security27')",f'root=Path({str(p)!r})'.replace('Path(', 'pathlib.Path(')).replace("str(root/'kubeconfig-deploy')","'/workspaces/projects/security27-continuation/kubeconfig'")
(p/'refresh_inventory.py').write_text(source)
subprocess.run(['python3',str(p/'refresh_inventory.py')],check=True)
