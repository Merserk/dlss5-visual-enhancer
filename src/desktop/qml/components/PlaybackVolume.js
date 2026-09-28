.pragma library

// Qt audio outputs take linear gain. Normalize Qt's logarithmic volume curve
// (whose linear-to-log range ends at 0.99) so the UI has exact 0/100 endpoints
// and every integer slider value survives conversion back from audio gain.
var logarithmicRange = 0.99
var logarithmicBase = Math.log(100)

function gainForLevel(level) {
    if (!isFinite(level)) return 0
    var position = Math.max(0, Math.min(100, level)) / 100
    if (position === 0) return 0
    if (position === 1) return 1
    return -Math.log(1 - logarithmicRange * position) / logarithmicBase
}

function levelForGain(gain) {
    if (!isFinite(gain)) return 0
    var linear = Math.max(0, Math.min(1, gain))
    if (linear === 0) return 0
    if (linear === 1) return 100
    return Math.round((1 - Math.exp(-linear * logarithmicBase)) / logarithmicRange * 100)
}
