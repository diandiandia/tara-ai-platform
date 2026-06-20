import { useI18n } from '../stores/i18nStore';
import { Handle, Position, NodeResizer } from 'reactflow';
import { User, Cpu, Database, Shield, Link } from 'lucide-react';
import { useCanvasStore } from '../stores/canvasStore';

export default function CustomDfdNode({ type, data, selected }) {
  const { t } = useI18n();
  const isReadOnly = useCanvasStore((state) => state.isReadOnly);
  const fontSize = data.fontSize || 11;
  const iconSize = Math.max(10, Math.round(fontSize * 1.25));
  const iconContainerSize = Math.max(16, Math.round(fontSize * 1.9));

  // Define node styles based on type (optimized for light mode canvas)
  let containerStyle;
  let icon;
  let textColor = '#0f172a'; // Slate 900
  let subTextColor = '#475569'; // Slate 600
  let iconBg = 'rgba(0, 0, 0, 0.05)';

  switch (type) {
    case 'entity':
      containerStyle = {
        background: '#eef2ff', // Indigo 50
        borderColor: '#4f46e5', // Indigo 600
        borderStyle: 'solid',
        borderRadius: '8px',
      };
      textColor = '#1e1b4b'; // Indigo 950
      subTextColor = '#4f46e5'; // Indigo 600
      iconBg = 'rgba(79, 70, 229, 0.1)';
      icon = <User size={iconSize} style={{ color: '#4f46e5' }} />;
      break;
    case 'process':
      containerStyle = {
        background: '#fdf2f8', // Pink 50
        borderColor: '#db2777', // Pink 600
        borderStyle: 'solid',
        borderRadius: '50%',
      };
      textColor = '#500724'; // Pink 950
      subTextColor = '#db2777'; // Pink 600
      iconBg = 'rgba(219, 39, 119, 0.1)';
      icon = <Cpu size={iconSize} style={{ color: '#db2777' }} />;
      break;
    case 'storage':
      containerStyle = {
        background: '#ecfdf5', // Emerald 50
        borderColor: '#059669', // Emerald 600
        borderStyle: 'solid',
        borderLeft: 'none',
        borderRight: 'none',
        borderRadius: '0px',
      };
      textColor = '#022c22'; // Emerald 950
      subTextColor = '#059669'; // Emerald 600
      iconBg = 'rgba(5, 150, 105, 0.1)';
      icon = <Database size={iconSize} style={{ color: '#059669' }} />;
      break;
    case 'boundary':
      containerStyle = {
        background: 'rgba(254, 242, 242, 0.3)', // Red 50 semi-transparent
        borderColor: '#dc2626', // Red 600
        borderStyle: 'dashed',
        borderRadius: '10px',
      };
      textColor = '#991b1b'; // Red 800
      subTextColor = 'rgba(153, 27, 27, 0.7)';
      iconBg = 'transparent';
      icon = <Shield size={iconSize} style={{ color: '#dc2626' }} />;
      break;
    case 'interface':
      containerStyle = {
        background: '#fffbeb', // Amber 50
        borderColor: '#d97706', // Amber 600
        borderStyle: 'solid',
        borderRadius: '8px',
      };
      textColor = '#78350f'; // Amber 950
      subTextColor = '#d97706'; // Amber 600
      iconBg = 'rgba(217, 119, 6, 0.1)';
      icon = <Link size={iconSize} style={{ color: '#d97706' }} />;
      break;
    default:
      containerStyle = {
        background: '#f8fafc',
        borderColor: '#64748b',
        borderStyle: 'solid',
        borderRadius: '8px',
      };
      icon = <Cpu size={iconSize} style={{ color: '#64748b' }} />;
  }

  const baseStyle = {
    width: '100%',
    height: '100%',
    minWidth: 'inherit',
    minHeight: 'inherit',
    borderWidth: type === 'boundary' ? '2px' : '2px',
    boxSizing: 'border-box',
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'center',
    alignItems: 'center',
    padding: type === 'boundary' ? '10px' : '6px',
    color: textColor,
    boxShadow: selected ? '0 0 0 3px rgba(79, 70, 229, 0.25)' : '0 4px 10px rgba(15, 23, 42, 0.08)',
    position: 'relative',
    transition: 'border-color 0.15s ease, box-shadow 0.15s ease',
    ...containerStyle
  };

  const handleStyle = {
    width: '8px',
    height: '8px',
    background: '#1e293b',
    border: '2px solid',
    borderRadius: '50%',
    zIndex: 10,
  };

  return (
    <div className={`custom-node-container ${type}-node ${selected ? 'selected' : ''}`} style={baseStyle}>
      {/* Node Resizer */}
      <NodeResizer 
        minWidth={type === 'process' ? 80 : 100} 
        minHeight={type === 'process' ? 80 : 50} 
        isVisible={selected && !isReadOnly}
        lineStyle={{ borderColor: 'var(--primary)', borderWidth: 1.5 }}
        handleStyle={{ width: 8, height: 8, background: 'var(--primary)', border: '1px solid #fff', borderRadius: '2px' }}
      />

      {/* Handles on 4 sides (source and target offset slightly to allow clean bidirectional routing) */}
      
      {/* Top side */}
      <Handle
        type="target"
        position={Position.Top}
        id="t-top"
        style={{ ...handleStyle, left: '50%' }}
      />
      <Handle
        type="source"
        position={Position.Top}
        id="s-top"
        style={{ ...handleStyle, left: '50%' }}
      />

      {/* Bottom side */}
      <Handle
        type="target"
        position={Position.Bottom}
        id="t-bottom"
        style={{ ...handleStyle, left: '50%' }}
      />
      <Handle
        type="source"
        position={Position.Bottom}
        id="s-bottom"
        style={{ ...handleStyle, left: '50%' }}
      />

      {/* Left side */}
      <Handle
        type="target"
        position={Position.Left}
        id="t-left"
        style={{ ...handleStyle, top: '50%' }}
      />
      <Handle
        type="source"
        position={Position.Left}
        id="s-left"
        style={{ ...handleStyle, top: '50%' }}
      />

      {/* Right side */}
      <Handle
        type="target"
        position={Position.Right}
        id="t-right"
        style={{ ...handleStyle, top: '50%' }}
      />
      <Handle
        type="source"
        position={Position.Right}
        id="s-right"
        style={{ ...handleStyle, top: '50%' }}
      />

      {/* Node Content */}
      <div style={{
        display: 'flex',
        flexDirection: (type === 'boundary' || type === 'process') ? 'column' : 'row',
        alignItems: 'center',
        justifyContent: (type === 'boundary' || type === 'process') ? 'center' : 'flex-start',
        gap: '6px',
        width: '100%',
        height: '100%',
        textAlign: (type === 'boundary' || type === 'process') ? 'center' : 'left',
        pointerEvents: 'none', // Allow clicks to go to react-flow node wrapper
        overflow: 'hidden'
      }}>
        {type === 'boundary' ? (
          <div style={{ display: 'flex', flexDirection: 'column', width: '100%', height: '100%', justifyContent: 'flex-start', alignItems: 'flex-start' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: `${Math.max(8, fontSize - 1)}px`, fontWeight: 'bold', color: textColor, textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: '4px' }}>
              {icon}
              <span>{data.name || t('物理边界')}</span>
            </div>
            {data.description && (
              <span style={{ fontSize: `${Math.max(8, fontSize - 2)}px`, color: subTextColor, textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                {data.description}
              </span>
            )}
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: `${iconContainerSize}px`, height: `${iconContainerSize}px`, flexShrink: 0, borderRadius: type === 'process' ? '50%' : '6px', background: iconBg, border: '1px solid rgba(0,0,0,0.05)' }}>
              {icon}
            </div>
            
            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px', width: '100%', overflow: 'hidden', textAlign: type === 'process' ? 'center' : 'left' }}>
              <span style={{ 
                fontSize: `${fontSize}px`, 
                fontWeight: '600', 
                color: textColor, 
                textOverflow: 'ellipsis', 
                overflow: 'hidden', 
                whiteSpace: 'normal',
                wordBreak: 'break-word',
                display: '-webkit-box',
                WebkitLineClamp: 2,
                WebkitBoxOrient: 'vertical',
                lineHeight: '1.2'
              }}>
                {data.name}
              </span>
              
              {data.protocol && (
                <span style={{ fontSize: `${Math.max(8, fontSize - 2)}px`, color: subTextColor, fontFamily: 'monospace', lineHeight: '1.1' }}>
                  {data.protocol}
                </span>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
