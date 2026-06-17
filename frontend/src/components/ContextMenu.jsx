import { useEffect, useRef } from 'react';

export default function ContextMenu({ onClose, options, position }) {
  const menuRef = useRef(null);

  useEffect(() => {
    const handlePointerDown = (event) => {
      if (!menuRef.current?.contains(event.target)) {
        onClose();
      }
    };

    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [onClose]);

  if (!position) {
    return null;
  }

  return (
    <div
      className="context-menu"
      ref={menuRef}
      style={{ left: position.x, top: position.y }}
      role="menu"
    >
      {options.map((option) => (
        <button
          className={`context-menu-item ${option.danger ? 'context-menu-item-danger' : ''}`}
          key={option.label}
          type="button"
          role="menuitem"
          onClick={() => {
            option.action();
            onClose();
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
