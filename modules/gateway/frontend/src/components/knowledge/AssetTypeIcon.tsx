import { LineIcon } from '@/components/ui/LineIcon';

const paths: Record<string, string> = {
  repo: 'M4 4h13a2 2 0 0 1 2 2v15H6a3 3 0 0 1-3-3V5a1 1 0 0 1 1-1M3 18a3 3 0 0 1 3-3h13M8 4v6l3-2 3 2V4',
  url: 'M9 15l6-6M7 10l-2 2a4 4 0 0 0 6 6l2-2M17 14l2-2a4 4 0 0 0-6-6l-2 2',
  doc: 'M5 3h10l4 4v14H5zM14 3v5h5M8 12h8M8 16h7',
  folder: 'M3 7V5h6l2 2h10v13H3z',
};

export function AssetTypeIcon({ type }: { type: string }) {
  return (
    <LineIcon className="h-5 w-5 shrink-0 text-gray-500 dark:text-gray-400">
      <path d={paths[type] ?? paths.folder} />
    </LineIcon>
  );
}
